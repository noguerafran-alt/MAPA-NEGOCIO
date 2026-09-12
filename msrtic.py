# -*- coding: utf-8 -*-
r"""MS RTIC: consumo de Jet A-1 por ruta y aerolinea, con las partidas del dia.

QUE ES
------
El mapa dibuja el historico de ANAC, que llega con meses de atraso y viene
agregado por ruta-mes: una ruta, un numero de vuelos, un numero de pasajeros. No
dice quien volo. MS RTIC es la otra punta: las partidas que Aeropuertos
Argentina publica HOY, una por una, con su aerolinea y su matricula.

Eso permite lo que el historico no puede: **abrir una ruta en las aerolineas que
la operan** y ponerle a cada una su consumo.

DE DONDE SALE CADA COSA -- son tres fuentes y ninguna es de este repo
--------------------------------------------------------------------
  1. RADAR YPF/aa2000_oficial.db        las partidas (numero, aerolinea, destino,
                                        matricula, pasajeros, hora real)
  2. RADAR YPF/tools/aircraft_db.sqlite matricula -> tipo de avion (registro de
                                        OpenSky, 609.357 aeronaves)
  3. RADAR YPF/ypf_clientes.json        que partidas abastece YPF

Y el consumo lo pone este repo, con `avion_model`: el mismo modelo del mapa.
No hay un segundo calculo de combustible.

POR QUE SOLO LEE, Y NUNCA PIDE NADA POR RED
-------------------------------------------
El market share es HTTP contra la API de AA2000. Este modulo NO la llama: la
regla 2 de CLAUDE.md dice que el mapa arranca sin internet, y sondear desde aca
la rompe. El radar sondea afuera cada 5 minutos y deja la base; el mapa la lee.
El "tiempo real" lo da el radar, no el mapa.

Si la base no esta -- el caso del kiosco, donde el radar no corre -- el filtro
simplemente no se ofrece. El mapa no cambia en nada.

LA CADENA, MEDIDA CONTRA EL FEED EN VIVO EL 2026-09-07
------------------------------------------------------
    492 partidas publicadas
    205 con matricula          (42%)
    191 con tipo resuelto      (39% del total, 93% de las que traen matricula)
    187 con tipo que el mapa sabe consumir (38% del total)

O sea que **6 de cada 10 partidas no publican matricula**. Para esas no se
inventa un avion: se usa el MISMO criterio que el mapa ya aplica al historico
(`seleccionar_avion` por ocupacion y distancia) y la fila queda marcada como
estimada. Cada fila dice de donde salio su avion, y `estado()` reporta el
reparto: un total de m3 que no distingue medido de estimado no se puede auditar.

Los tres tipos que quedaron afuera de `flota.json` son regionales chicos (SF34,
E145, DHC6). Se cuentan en el estado en vez de descartarse en silencio.

DESCONOCIDO NO ES COMPETENCIA
-----------------------------
La clasificacion la hace `market_share.clasificar()` del radar -- se importa, no
se copia: dos implementaciones del mismo share dan numeros distintos un dia y
nadie sabe cual creer. Devuelve 'ypf', 'competencia' o 'sin_clasificar', y esa
tercera categoria se mantiene separada hasta el final. Si la lista de clientes no
se declara exhaustiva, una aerolinea que no figura PUEDE ser de YPF sin estar
anotada: contarla como competencia bajaria el share de YPF sin evidencia.

Por eso el resumen da un piso y un techo, no un numero.

RUTAS CONFIGURABLES
-------------------
Por defecto se busca el radar en `../RADAR YPF` (al lado de este repo). Se puede
mover con variables de entorno:

    MS_RTIC_RADAR      carpeta del radar (de ahi salen las tres fuentes)
    MS_RTIC_OFICIAL    la base de partidas, si esta en otro lado
    MS_RTIC_AVIONES    la base de matriculas
    MS_RTIC_CLIENTES   ypf_clientes.json
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import io
import json
import re
import sqlite3
import threading
import time

import avion_model

BASE = os.path.dirname(os.path.abspath(__file__))
IATA_JSON = os.path.join(BASE, 'datos', 'iata_aeropuertos.json')

# Densidad del Jet A-1 que usa todo el repo para pasar de toneladas a m3.
DENSIDAD_T_M3 = 0.8

# El radar, al lado de este repo. No se importa nada de ahi salvo market_share, y
# eso con importlib: el radar no es un paquete instalable y agregar su carpeta al
# sys.path arrastraria sus modulos de camara y de dongle.
# ACA NO HAY RADAR AL LADO: esto corre en Render, no en la PC de la antena. Las dos
# bases viven en el DISCO PERSISTENTE (/var/data), no en el repo:
#
#   - aa2000_oficial.db  la escribe el poller de este mismo proceso (ver sondeo.py).
#   - aircraft_db.sqlite pesa 52 MB y se construye una vez desde OpenSky. El volcado
#                        COMPLETO sigue viviendo solo en el disco: son 52 MB y 9
#                        columnas. Lo que si viaja en el repo es la semilla recortada
#                        de datos/ (dos columnas, ~16 MB) -- ver base_aviones().
#
# Que esten en el disco y no en el repo es lo que hace que el acumulado sobreviva a los
# deploys: el filesystem de Render es efimero salvo el disco montado.
DISCO = os.environ.get('RENDER_DISK_PATH', '/var/data')


def base_oficial():
    return os.environ.get('MS_RTIC_OFICIAL') or os.path.join(DISCO, 'aa2000_oficial.db')


SEMILLA_AVIONES = os.path.join(BASE, 'datos', 'aircraft_db.sqlite')


def base_aviones():
    """El registro de matriculas: disco > semilla del repo > nada.

    EL DEL DISCO GANA cuando esta, porque es el volcado completo (609.357 filas) que
    alguien subio a mano a /var/data. La semilla de datos/ trae hoy las 504.757
    matriculas con typecode del mismo volcado (alcance 'todo', ~16 MB), para que un
    deploy nuevo -- sin nada todavia en el disco persistente -- resuelva tambien los
    extranjeros en vez de caer a "avion no identificado". La diferencia con el disco
    son las filas SIN typecode, que el lookup de aca abajo descarta igual.

    ESOS 16 MB VIAJAN EN CADA CLONE Y CADA DEPLOY. Es una decision tomada a
    proposito (2026-09-12), no un descuido: se prefirio cobertura completa desde el
    primer arranque antes que un repo liviano. Si algun dia pesa de mas, volver a la
    semilla chica es correr el script con alcance 'ar' (~1.700 filas, 68 KB).
    Se regenera con datos/actualizar_registro_aviones.py.
    """
    del_entorno = os.environ.get('MS_RTIC_AVIONES')
    if del_entorno:
        return del_entorno
    del_disco = os.path.join(DISCO, 'aircraft_db.sqlite')
    if os.path.exists(del_disco):
        return del_disco
    return SEMILLA_AVIONES if os.path.exists(SEMILLA_AVIONES) else del_disco


PROVEEDORES_SEMILLA = os.path.join(BASE, 'datos', 'proveedores.json')


def tabla_proveedores():
    """La tabla vigente: la del DISCO si alguien subio una planilla, si no la semilla.

    Los dos lugares hacen falta y no son redundantes:

      disco  lo que se sube desde /quien-cargo. Tiene que estar ahi y no en el repo
             porque el filesystem de Render es efimero: escrito al lado del codigo,
             el proximo deploy lo borraria y la pantalla volveria sola a una planilla
             vieja sin avisar.
      repo   la semilla, para que un deploy nuevo arranque con datos en vez de una
             tabla vacia.

    Es el unico dato no publico de todo el modulo -- dice que rutas abastece cada
    petrolera -- y por eso /api/msrtic y /quien-cargo exigen nivel 1.
    """
    if os.environ.get('MS_RTIC_PROVEEDORES'):
        return os.environ['MS_RTIC_PROVEEDORES']
    del_disco = os.path.join(DISCO, 'proveedores.json')
    return del_disco if os.path.exists(del_disco) else PROVEEDORES_SEMILLA


def tabla_proveedores_destino():
    """Donde se ESCRIBE una planilla nueva. Siempre el disco, nunca el repo."""
    return os.environ.get('MS_RTIC_PROVEEDORES') or os.path.join(
        DISCO, 'proveedores.json')


_cache = {'clave': None, 'filas': None, 'estado': None}
_lock = threading.Lock()


def _norm_matricula(s):
    """LV-FUA, 'lv fua' y LVFUA son la misma matricula.

    AA2000 la publica sin separadores ('LVFUA') y OpenSky con guion ('LV-FUA').
    Sin normalizar el cruce da 0%, y lo da EN SILENCIO: se ve igual que "ningun
    vuelo publico matricula".
    """
    return re.sub(r'[^A-Z0-9]', '', (s or '').upper())


def _cargar_iata():
    with io.open(IATA_JSON, encoding='utf-8-sig') as f:
        d = json.load(f)
    tabla = {}
    for grupo, arg in (('argentinos', True), ('internacionales', False)):
        for k, v in (d.get(grupo) or {}).items():
            tabla[k.strip().upper()] = (v, arg)
    return tabla


def _cargar_proveedores():
    """El clasificador. Aca viaja EN el repo, asi que es un import normal.

    En el mapa local se carga por ruta con importlib porque vive en otro repo; aca
    proveedores.py se copio adentro, que es lo unico que puede funcionar en un deploy.
    """
    try:
        import proveedores
        return proveedores
    except Exception:
        return None


def disponible():
    """Si hay con que armar el filtro. Sin la base de partidas no hay nada."""
    return os.path.exists(base_oficial())


def registro_disponible():
    """Si hay algun archivo de matriculas contra el que cruzar.

    Distinto de "esta matricula puntual no esta anotada": si esto da False, NINGUNA
    matricula va a resolver nunca, y eso tiene que llegar a la pantalla distinto de
    "la matricula no estaba en el registro" -- lo primero es un problema del deploy,
    lo segundo es una laguna del registro.
    """
    return os.path.exists(base_aviones())


def _tipos_por_matricula(matriculas):
    """{matricula normalizada: typecode} para las que esten en el registro.

    Una sola pasada con todas las matriculas del periodo, no una consulta por
    fila: son ~600.000 aeronaves y abrir la base por vuelo cuesta mas que todo el
    resto junto.

    Si el archivo no existe devuelve {} EN SILENCIO -- eso es a proposito, el
    llamador tiene que consultar `registro_disponible()` aparte para saber si el
    {} vacio significa "no hay archivo" o "ninguna matricula de esta tanda esta
    anotada".
    """
    path = base_aviones()
    if not matriculas or not os.path.exists(path):
        return {}
    con = sqlite3.connect('file:%s?mode=ro' % path.replace(os.sep, '/'), uri=True)
    try:
        out = {}
        for reg, typ in con.execute(
                'select registration, typecode from aircraft '
                'where registration is not null and typecode is not null'):
            n = _norm_matricula(reg)
            if n in matriculas and n not in out:
                out[n] = typ.strip().upper()
        return out
    finally:
        con.close()


def _coords():
    """{nombre: (lat, lon)} de la base del mapa, para poder medir distancias."""
    from models import Airport
    return {a.name: (a.lat, a.lon) for a in Airport.query.all()
            if a.lat is not None and a.lon is not None}


def dia_de(partida):
    """El dia LOCAL en que la partida despego, 'AAAA-MM-DD', o None.

    La hora REAL manda sobre la programada: un vuelo programado 23:50 que sale 00:20
    cargo combustible el dia siguiente, y para "cuanto se vendio el dia 8" lo que cuenta
    es cuando salio, no cuando estaba previsto.

    Y es dia local, no UTC. AA2000 publica en hora argentina y la pregunta -- "cuanto se
    vendio el 8 de septiembre" -- es sobre el dia del calendario de acá; agrupando por
    UTC, todo lo que sale despues de las 21:00 se contaria al dia siguiente.
    """
    ep = partida.get('real_epoch') or partida.get('programada_epoch')
    if not ep:
        return None
    return time.strftime('%Y-%m-%d', time.localtime(ep))


def dias_disponibles():
    """[{'dia', 'partidas', 'despegadas'}] de mas nuevo a mas viejo.

    Sirve para poblar el selector con lo que EXISTE en vez de un calendario donde casi
    todas las fechas no tienen nada. Y trae el conteo porque un dia con 30 partidas y
    otro con 700 no se pueden leer igual: el primero esta a medio sondear.
    """
    cuenta = {}
    for p in _partidas(None):
        d = dia_de(p)
        if not d:
            continue
        c = cuenta.setdefault(d, {'dia': d, 'partidas': 0, 'despegadas': 0})
        c['partidas'] += 1
        if p.get('real_epoch'):
            c['despegadas'] += 1
    return sorted(cuenta.values(), key=lambda c: c['dia'], reverse=True)


def _partidas(horas):
    """Las partidas de la base del radar.

    Solo despegues: el avion carga combustible ANTES de irse, asi que la operacion
    que importa para el negocio es la partida. Es la misma decision que toma el
    market share del radar.
    """
    path = base_oficial()
    con = sqlite3.connect('file:%s?mode=ro' % path.replace(os.sep, '/'), uri=True)
    con.row_factory = sqlite3.Row
    try:
        sql = "select * from vuelo_oficial where movimiento='D'"
        args = []
        if horas:
            # programada_epoch y no ultima_vez: interesa cuando SALE el vuelo, no
            # cuando el sondeo lo vio por ultima vez.
            sql += ' and programada_epoch >= ?'
            args.append(time.time() - horas * 3600.0)
        return [dict(r) for r in con.execute(sql, args)]
    finally:
        con.close()


def calcular(horas=24.0, coords=None, dia=None, desde=None, hasta=None):
    """(filas, estado). Una fila por ruta-aerolinea, con vuelos, pax y m3.

    `horas` mira hacia atras desde ahora sobre la hora PROGRAMADA de salida. 24 es
    el dia movil; None trae todo lo acumulado.
    """
    iata = _cargar_iata()
    ms = _cargar_proveedores()
    lista = None
    # Tres estados distintos, y hay que poder decir cual es. `cargar_lista` NUNCA
    # lanza: cuando el archivo falta devuelve una lista vacia con existe=False, asi
    # que tratarla como valida hace que todo caiga en 'sin_clasificar' y el filtro
    # informe un share de 0 a 100 sin explicar que lo que falta es la lista.
    clasif = 'sin_modulo'
    if ms is not None:
        try:
            lista = ms.cargar_tabla(tabla_proveedores())
        except Exception:
            lista = None
        if lista is None:
            clasif = 'error'
        elif not lista.get('existe'):
            # Sin planilla igual se clasifica: quien_cargo() resuelve TODO EL INTERIOR
            # sin mirarla -- si la partida no sale de AEP/EZE/COR es de YPF por
            # estructura del mercado. Lo que queda sin resolver son las competitivas.
            clasif = 'sin_planilla'
        else:
            clasif = 'planilla'

    # Con un dia pedido se ignora la ventana de horas: son dos formas distintas de
    # cortar lo mismo y combinarlas daria un subconjunto que nadie pidio.
    # Tres formas de cortar el mismo dato, y se aplica UNA. El rango manda sobre el dia y
    # el dia sobre la ventana de horas: combinarlas daria un subconjunto que nadie pidio.
    rango = bool(desde or hasta)
    crudas = _partidas(None if (dia or rango) else horas)
    if rango:
        # Inclusivo en las dos puntas: "del 3 al 8" incluye el 3 y el 8, que es lo que
        # significa en castellano.
        d0, d1 = (desde or '0000-00-00'), (hasta or '9999-99-99')
        if d0 > d1:
            d0, d1 = d1, d0     # fechas al reves: se ordenan en vez de devolver vacio
        crudas = [p for p in crudas if (dia_de(p) or '') and d0 <= dia_de(p) <= d1]
    elif dia:
        crudas = [p for p in crudas if dia_de(p) == dia]
    coords = _coords() if coords is None else coords

    # SOLO PARTIDAS CONFIRMADAS: con hora de despegue MEDIDA (real_epoch). Mismo
    # criterio, mismo motivo, que quien_cargo.desde_base() -- ver el docstring de
    # ese modulo. Esta funcion alimenta la planilla de consumo (m3, vuelos por
    # ruta-aerolinea), asi que no puede contar partidas que AA2000 todavia no vio
    # despegar: eso duplicaria, con otro numero, la decision que quien_cargo ya
    # tomo y documento por escrito.
    #
    # OJO: este filtro es SOLO para calcular(). en_el_aire(), mas abajo, dibuja
    # aviones volando AHORA y por diseno usa la hora PROGRAMADA cuando AA2000
    # todavia no publico la real -- ver ESTADOS_YA_SALIO y su docstring. Aplicarle
    # este mismo filtro vaciaria el mapa en vivo. Son dos preguntas distintas
    # ("cuanto se cargo" vs "que esta volando ahora") y comparten _partidas() pero
    # no este corte.
    partidas_totales = len(crudas)
    despegadas_total = sum(1 for p in crudas if p.get('real_epoch'))
    crudas = [p for p in crudas if p.get('real_epoch')]

    mats = {_norm_matricula(p.get('matricula')) for p in crudas}
    mats.discard('')
    tipos = _tipos_por_matricula(mats)
    flota = avion_model.get_flota()

    est = {'partidas': len(crudas), 'dia': dia, 'desde': desde, 'hasta': hasta,
           # Cuantos vuelos llevaron correccion por su ocupacion real.
           'ajustados_por_pax': 0,
           # Cuantas de las partidas del periodo ya despegaron. Coincide con
           # 'partidas' porque 'crudas' ya viene filtrada a solo confirmadas -- se
           # deja el campo para no romper a quien lo consuma, pero el numero que
           # importa ahora es 'partidas_totales' vs 'partidas'.
           'despegadas': despegadas_total,
           # Cuantas partidas del periodo NO tienen hora real todavia (programadas,
           # demoradas, etc.) y por eso quedaron afuera de este calculo. Mismo
           # concepto que 'programadas_sin_ocurrir' + 'no_salieron' en quien_cargo.
           'partidas_totales': partidas_totales,
           'sin_confirmar': partidas_totales - despegadas_total,
           'con_matricula': 0, 'tipo_resuelto': 0,
           'sin_flota': 0, 'avion_medido': 0, 'avion_estimado': 0,
           'sin_ruta': 0, 'sin_consumo': 0, 'sin_pax': 0,
           'iata_desconocidos': {}, 'tipos_sin_flota': {},
           # 'sin_modulo' | 'error' | 'sin_lista' | 'parcial' | 'exhaustiva'.
           # Con 'parcial' el share es un rango por definicion; con 'sin_lista' no
           # hay share posible y el rango va de 0 al total.
           'clasificador': clasif,
           'tabla_proveedores': tabla_proveedores(),
           'horas': horas}

    acum = {}
    # Las coordenadas viajan CON la fila y no las busca el frontend. El mapa las tiene
    # en DATA[tipo].meta, pero solo de las rutas que ANAC informo: una ruta nueva que
    # aparece hoy en el feed no esta ahi, y es justo el caso que este filtro existe
    # para mostrar.
    coords_ruta = {}
    for p in crudas:
        cod_o = (p.get('aeropuerto') or '').strip().upper()
        cod_d = (p.get('otro_aeropuerto') or '').strip().upper()
        origen, destino = iata.get(cod_o), iata.get(cod_d)
        if not origen or not destino:
            # Un IATA que no esta en la tabla NO se adivina: se cuenta y se nombra,
            # para que agregarlo sea una linea de JSON y no una investigacion.
            est['sin_ruta'] += 1
            for cod in (cod_o, cod_d):
                if cod and cod not in iata:
                    est['iata_desconocidos'][cod] = \
                        est['iata_desconocidos'].get(cod, 0) + 1
            continue

        o_nombre, o_arg = origen
        d_nombre, d_arg = destino
        if o_nombre == d_nombre:
            est['sin_ruta'] += 1
            continue

        c_o, c_d = coords.get(o_nombre), coords.get(d_nombre)
        if not c_o or not c_d:
            est['sin_ruta'] += 1
            continue
        dist = avion_model.haversine(c_o[0], c_o[1], c_d[0], c_d[1])
        key = avion_model._key(o_nombre, d_nombre)

        pax = p.get('pasajeros')
        try:
            pax = int(pax) if pax not in (None, '') else None
        except (TypeError, ValueError):
            pax = None
        if pax is None:
            est['sin_pax'] += 1

        # El avion: primero el REAL, por matricula. Es todo el aporte del registro
        # de la antena, y es lo que separa esto de una estimacion.
        mat = _norm_matricula(p.get('matricula'))
        if mat:
            est['con_matricula'] += 1
        codigo = tipos.get(mat)
        if codigo:
            est['tipo_resuelto'] += 1
        medido = bool(codigo and codigo in flota)
        if codigo and not medido:
            est['sin_flota'] += 1
            est['tipos_sin_flota'][codigo] = est['tipos_sin_flota'].get(codigo, 0) + 1
        if medido:
            est['avion_medido'] += 1
        else:
            # Mismo criterio que el mapa usa para el historico. No es un invento
            # nuevo: es la funcion que ya elige el avion de cada ruta-mes.
            codigo = avion_model.seleccionar_avion(dist, pax, key)
            if codigo:
                est['avion_estimado'] += 1

        m3 = None
        if codigo:
            tons, _fuente = avion_model.consumo_toneladas(codigo, dist, key)
            if tons is not None:
                # CORRECCION POR OCUPACION REAL DEL VUELO. La planilla de consumo esta
                # armada asumiendo un factor de ocupacion de referencia (LF_REFERENCIA,
                # 0,82): un vuelo que sale con 95% pesa mas y quema mas, y uno con 40%
                # quema menos. Hasta ahora esta cuenta ignoraba eso y le ponia a TODOS
                # los vuelos el consumo del avion medio lleno.
                #
                # No es un modelo nuevo: es `ajuste_por_ocupacion()`, la misma funcion
                # que ya corrige las proyecciones del mapa (app.py). Lo que cambia es
                # que aca el pax es el REAL de esa partida, publicado por AA2000, y no
                # un promedio mensual -- que es justamente lo que esta pantalla tiene y
                # el historico no.
                #
                # SOLO CUANDO EL AVION ES EL REAL. Con el avion estimado, la ocupacion
                # se calcularia contra los asientos de un tipo que elegimos nosotros:
                # `seleccionar_avion()` ya usa el pax para elegirlo, asi que corregir
                # ademas por ocupacion contaria el mismo dato dos veces.
                factor = 1.0
                if medido and pax:
                    factor, _lf = avion_model.ajuste_por_ocupacion(codigo, pax)
                    if factor != 1.0:
                        est['ajustados_por_pax'] += 1
                m3 = tons * factor / DENSIDAD_T_M3
        if m3 is None:
            est['sin_consumo'] += 1

        # QUIEN ABASTECE ES PROPIEDAD DE LA RUTA -- la planilla de YPF esta armada por
        # ruta dirigida, asi que si dos aerolineas hacen AEP-BRC las dos cargan con el
        # mismo -- SALVO EXCEPCION DECLARADA. Desde el 2026-09-09 se puede declarar que
        # una compania se aparta del proveedor de su ruta, y por eso aca se pasa la
        # aerolinea: sin pasarla, una excepcion existiria en la pantalla de "Quien le
        # cargo" y seria invisible en el mapa, que es peor que no tenerla -- dos
        # pantallas del mismo sistema afirmando cosas distintas sobre el mismo vuelo.
        #
        # Casi siempre no cambia nada: sin excepcion declarada, la respuesta es la de la
        # ruta. Pasarla nunca empeora la respuesta, como mucho la precisa.
        proveedor, motivo = None, 'sin_clasificador'
        if ms is not None and lista is not None:
            try:
                q = ms.quien_cargo(cod_o, cod_d, lista,
                                   (p.get('aerolinea_id') or '').strip().upper() or None)
                proveedor, motivo = q.get('proveedor'), q.get('motivo')
            except TypeError:
                # Clasificador viejo, sin el 4to argumento: se sigue clasificando por
                # ruta. Degradar es mejor que quedarse sin proveedor en todo el mapa.
                q = ms.quien_cargo(cod_o, cod_d, lista)
                proveedor, motivo = q.get('proveedor'), q.get('motivo')
            except Exception:
                proveedor, motivo = None, 'error'
        bandera = proveedor or 'sin_declarar'

        coords_ruta[(o_nombre, d_nombre)] = (c_o[0], c_o[1], c_d[0], c_d[1], dist)
        clave = ('cabotaje' if (o_arg and d_arg) else 'internacional',
                 o_nombre, d_nombre,
                 (p.get('aerolinea') or p.get('aerolinea_id') or 'Sin identificar').strip(),
                 (p.get('aerolinea_id') or '').strip().upper(),
                 bandera, motivo)
        f = acum.setdefault(clave, {'vuelos': 0, 'pax': 0, 'con_pax': 0,
                                    'con_pax_pos': 0, 'm3': 0.0,
                                    'medidos': 0, 'estimados': 0, 'aviones': {},
                                    # EL IATA DE LA RUTA, que la planilla de proveedores
                                    # usa como clave ('EZE-MAD'). La fila agrupa por
                                    # NOMBRE de aeropuerto, asi que sin esto quien quiera
                                    # editar la planilla desde una fila no tiene con que:
                                    # 'Ezeiza' no es una clave de la tabla.
                                    'cod_o': cod_o, 'cod_d': cod_d})
        f['vuelos'] += 1
        if pax is not None:
            f['pax'] += pax
            f['con_pax'] += 1
            # Y aparte los que informaron un pax MAYOR A CERO. En este feed un 0 es casi
            # seguro "no informado todavia" y no "volo vacio" -- lo dice aa2000.py, que
            # por eso distingue el 0 del vacio. Contar esos ceros como dato hunde la
            # ocupacion: EZE-MAD tenia 7 de 7 vuelos "informados", todos en 0, y daba
            # una ocupacion de 0,0 pax/vuelo para un A330 lleno. Con este conteo aparte
            # la ocupacion se calcula sobre lo que de verdad se sabe.
            if pax > 0:
                f['con_pax_pos'] += 1
        if m3 is not None:
            f['m3'] += m3
        f['medidos' if medido else 'estimados'] += 1
        if codigo:
            f['aviones'][codigo] = f['aviones'].get(codigo, 0) + 1

    filas_out = []
    for (tipo, o, d, aero, aero_id, bandera, motivo), v in sorted(acum.items()):
        filas_out.append({
            'tipo': tipo, 'origin': o, 'dest': d,
            # Los IATA van al lado del nombre, no en su lugar: la pantalla muestra
            # "Ezeiza" y la planilla se indexa por "EZE".
            'cod_o': v['cod_o'], 'cod_d': v['cod_d'],
            'aerolinea': aero, 'aerolinea_id': aero_id,
            # 'YPF' | 'Axion' | 'Raizen' | 'sin_declarar'. El competidor va con su
            # nombre: la planilla lo nombra, asi que llamarlo "competencia" seria
            # perder informacion que ya se tiene.
            'proveedor': bandera,
            # Por que se afirma eso: 'planilla' es evidencia directa, 'interior' es
            # estructura del mercado (no sale de AEP/EZE/COR), y los demas son "no se
            # puede afirmar". Un proveedor sin su motivo deja indistinguible una cosa
            # de la otra, y son dos niveles de evidencia muy distintos.
            'motivo': motivo,
            'vuelos': v['vuelos'],
            # pax es la suma de las partidas QUE LO INFORMARON, no del total: si 3
            # de 10 vuelos publican pasajeros, sumar y presentarlo como el pax de la
            # ruta la subestima por siete. `vuelos_con_pax` deja verlo.
            'pax': v['pax'] if v['con_pax'] else None,
            'vuelos_con_pax': v['con_pax'],
            # Cuantos de esos informaron un numero distinto de cero: es el denominador
            # con el que la ocupacion significa algo.
            'vuelos_con_pax_pos': v['con_pax_pos'],
            'm3': round(v['m3'], 1),
            'aviones_medidos': v['medidos'],
            'aviones_estimados': v['estimados'],
            'aviones': sorted(v['aviones'], key=lambda c: -v['aviones'][c]),
            # Y CUANTAS VECES cada uno, no solo cuales. Con la lista sola, quien quiera
            # un promedio por avion (asientos, por ejemplo) tiene que elegir uno y
            # tratarlo como si hubiera volado todos los vuelos de la fila.
            'aviones_n': dict(v['aviones']),
        })
        cr = coords_ruta.get((o, d))
        if cr:
            filas_out[-1].update(olat=cr[0], olon=cr[1], dlat=cr[2], dlon=cr[3],
                                 distancia_km=round(cr[4], 1))
    return filas_out, est


# CUANTO TARDA UN VUELO, aproximado. La velocidad de crucero NO es la velocidad
# promedio: el rodaje, el ascenso y el descenso son mucho mas lentos, asi que volar toda
# la distancia a crucero deja al avion adelantado. Se le suma un margen fijo por las dos
# puntas.
#
# Es una aproximacion y se dice que lo es. No hay plan de vuelo ni viento: la ruta real
# sigue aerovias y es mas larga que la recta, y en Ezeiza-Madrid el jet stream cambia el
# tiempo de vuelo casi una hora segun la direccion. Esta aparte y con nombre para poder
# calibrarlo el dia que se compare contra horas de arribo reales.
MARGEN_PUNTAS_H = 25.0 / 60.0

# QUE ESTADOS DE AA2000 SIGNIFICAN QUE EL AVION YA SALIO, cuando su hora programada paso
# y todavia no hay hora real. Es lista BLANCA a proposito: lo que no esta nombrado no se
# dibuja volando. Medido el 2026-09-12 sobre 531 partidas en esa situacion: 366 "En
# Horario" y 25 "Cerrado" (salieron), contra 53 "Demorado", 38 "Pre Embarque", 20
# "Embarcando" y 11 "Ultimo aviso", que dicen que el avion sigue en tierra.
ESTADOS_YA_SALIO = ('en horario', 'cerrado')


def en_el_aire(ahora=None, horas=36.0):
    """Los vuelos que estarian volando ahora, con lo que arma "Quien le cargo".

    LA FUENTE ES `quien_cargo.tablero()`, no la base cruda. Es el mismo modulo que
    procesa el feed de AA2000 sin parar, y el que ya resolvio las dos cosas dificiles:
    en que SITUACION esta cada partida (despego / sin_dato / no_salio / programada) y
    QUE PETROLERA la abastece. Volver a derivar eso aca seria una segunda
    implementacion de las mismas reglas, y este repo ya sabe como termina: dos numeros
    distintos y nadie sabiendo cual creer. Ademas trae gratis el proveedor al tooltip.

    ESTO NO ES SEGUIMIENTO: es navegacion a estima. FlightRadar dibuja donde el avion
    DICE que esta, por ADS-B; esto dibuja donde deberia estar segun cuando salio y a
    que velocidad vuela su tipo.

    DOS FUENTES PARA LA HORA DE SALIDA, y la segunda existe porque la primera sola
    dejaba el cielo sin cabotaje. AA2000 publica la hora REAL con horas de retraso:
    medido el 2026-09-12 con el poller vivo, de 50 partidas programadas en las ultimas
    3 horas ninguna tenia hora real. Para un cabotaje eso es fatal -- cuando su hora se
    publica, ya aterrizo. Entonces tambien entran las `sin_dato` (hora programada
    pasada, sin hora real) cuyo estado no diga que siguen en tierra, y cada vuelo lleva
    `hora_base` para que la pantalla diga cual uso.

    La posicion NO se calcula aca: se mandan las dos puntas y los dos instantes, y el
    navegador interpola con su reloj. Asi los aviones se mueven con UN pedido cada
    tanto en vez de uno por cuadro.
    """
    import time as _t
    ahora = float(ahora if ahora is not None else _t.time())

    qc = _cargar_quien_cargo()
    if qc is None:
        return {'vuelos': [], 'estado': {'motivo': 'quien_cargo no disponible'},
                'ahora_epoch': ahora, 'margen_puntas_min': round(MARGEN_PUNTAS_H * 60)}
    prov = _cargar_proveedores()
    tabla = prov.cargar_tabla(tabla_proveedores()) if prov else None
    datos = qc.tablero(base_oficial(), horas, None, tabla)
    partidas = (datos or {}).get('vuelos') or []

    iata = _cargar_iata()
    coords = _coords()
    mats = {_norm_matricula(p.get('matricula')) for p in partidas}
    mats.discard('')
    tipos = _tipos_por_matricula(mats)
    flota = avion_model.get_flota()

    est = {'partidas': len(partidas), 'despegadas': 0, 'en_el_aire': 0,
           'por_hora_programada': 0, 'sin_hora_medida': 0, 'sin_ruta': 0,
           'aterrizados': 0, 'avion_medido': 0, 'avion_desconocido': 0,
           'con_pax': 0, 'iata_desconocidos': {},
           'registro_disponible': registro_disponible()}
    vuelos = []
    ultima_salida = 0

    for p in partidas:
        etiqueta = (p.get('estado') or '').strip().lower()
        salida, hora_base = None, None
        if p.get('situacion') == 'despego' and p.get('real_epoch'):
            salida, hora_base = p['real_epoch'], 'medida'
        elif p.get('situacion') == 'sin_dato' and etiqueta in ESTADOS_YA_SALIO:
            salida, hora_base = p.get('programada_epoch'), 'programada'
        if not salida:
            est['sin_hora_medida'] += 1
            continue
        est['despegadas'] += 1
        if salida > ultima_salida:
            ultima_salida = salida
        if hora_base == 'programada':
            est['por_hora_programada'] += 1

        cod_o = (p.get('origen') or '').strip().upper()
        cod_d = (p.get('destino') or '').strip().upper()
        origen, destino = iata.get(cod_o), iata.get(cod_d)
        if not origen or not destino:
            est['sin_ruta'] += 1
            for cod in (cod_o, cod_d):
                if cod and cod not in iata:
                    est['iata_desconocidos'][cod] = est['iata_desconocidos'].get(cod, 0) + 1
            continue
        o_nombre, d_nombre = origen[0], destino[0]
        c_o, c_d = coords.get(o_nombre), coords.get(d_nombre)
        if o_nombre == d_nombre or not c_o or not c_d:
            est['sin_ruta'] += 1
            continue
        dist = avion_model.haversine(c_o[0], c_o[1], c_d[0], c_d[1])

        codigo = tipos.get(_norm_matricula(p.get('matricula')))
        ficha = flota.get(codigo) if codigo else None
        est['avion_medido' if ficha else 'avion_desconocido'] += 1
        vel = (ficha or {}).get('velocidad_crucero_kmh') or 830.0
        llegada = salida + (MARGEN_PUNTAS_H + dist / float(vel)) * 3600.0
        if ahora >= llegada:
            est['aterrizados'] += 1
            continue
        if ahora < salida:
            continue

        pax = p.get('pasajeros')
        try:
            pax = int(pax) if pax not in (None, '') else None
        except (TypeError, ValueError):
            pax = None
        if not pax:
            pax = None          # un 0 de este feed es "no informado"
        else:
            est['con_pax'] += 1

        vuelos.append({
            'numero': p.get('numero'), 'aerolinea': p.get('aerolinea'),
            'aerolinea_id': p.get('aerolinea_id'), 'matricula': p.get('matricula'),
            'avion': (ficha or {}).get('nombre') or codigo,
            'avion_medido': bool(ficha),
            'origen': o_nombre, 'destino': d_nombre,
            'olat': c_o[0], 'olon': c_o[1], 'dlat': c_d[0], 'dlon': c_d[1],
            'distancia_km': round(dist, 1),
            'salida_epoch': salida, 'llegada_epoch': llegada,
            'hora_base': hora_base, 'estado': p.get('estado'),
            'velocidad_kmh': round(float(vel)),
            'pax': pax,
            'proveedor': p.get('proveedor'), 'motivo': p.get('motivo'),
        })

    # NO es max(real_epoch): AA2000 publica la hora real con horas de atraso, asi
    # que la mayoria de las partidas entran por hora PROGRAMADA (ver hora_base
    # arriba). Medir solo contra la hora real hacia decir "hace 3 h" con el feed
    # fresco -- lo viejo era la PUBLICACION de horas reales, no las partidas.
    # `ultima_salida` ya usa la mejor hora disponible por partida, la misma que
    # decide si una partida "ya salio".
    est['ultima_partida_epoch'] = ultima_salida or None
    est['en_el_aire'] = len(vuelos)
    vuelos.sort(key=lambda v: -v['llegada_epoch'])
    return {'vuelos': vuelos, 'estado': est, 'ahora_epoch': ahora,
            'margen_puntas_min': round(MARGEN_PUNTAS_H * 60)}


def _cargar_quien_cargo():
    """El modulo que procesa el feed. Guardado: sin el, la capa no se ofrece."""
    try:
        import quien_cargo
        return quien_cargo
    except Exception:                                    # noqa: BLE001
        return None


def _clave_de_archivo():
    """Mtime y tamano de las bases que se leen. Cambia cuando alguna se escribe.

    ACA HAY UNA SOLA BASE, la del disco de Render: no existe el caso de dos que si tiene
    el mapa local (el radar escribe la suya, el Excel del SharePoint completa la otra), y
    por eso no hay `bases_oficiales()` en este repo.

    Se escribe como lista igual, de una sola entrada, para que la copia local y esta no
    divergan en la forma: el 2026-09-10 se porteo el arreglo de alla tal cual y quedo
    llamando a `bases_oficiales()`, que aca no existe -- NameError en cada `filas()`, con
    `verificar_deploy.py` en verde porque no llamaba a `filas()`. Un porteo entre dos
    copias que no tienen la misma API no es un copy-paste.
    """
    partes = []
    for p in (base_oficial(),):
        try:
            st = os.stat(p)
            partes.append((p, st.st_mtime_ns, st.st_size))
        except OSError:
            partes.append((p, None, None))
    return tuple(sorted(partes)) or None


def filas(horas=24.0, usar_cache=True, dia=None, desde=None, hasta=None):
    """Las filas del filtro, cacheadas hasta que el radar vuelva a escribir."""
    clave = (_clave_de_archivo(), horas, dia, desde, hasta)
    with _lock:
        if usar_cache and _cache['clave'] == clave and _cache['filas'] is not None:
            return _cache['filas'], _cache['estado']
        fs, est = calcular(horas, dia=dia, desde=desde, hasta=hasta)
        _cache['clave'], _cache['filas'], _cache['estado'] = clave, fs, est
        return fs, est


def resumen(horas=24.0, dia=None, desde=None, hasta=None):
    """Lo que necesita el banner del mapa: totales y el rango de YPF.

    El share va como RANGO. Con la lista de clientes no exhaustiva, el piso son los
    m3 de los clientes confirmados y el techo suma los sin clasificar; dar un numero
    solo seria elegir uno de los dos sin decirlo.
    """
    fs, est = filas(horas, dia=dia, desde=desde, hasta=hasta)
    tot = sum(f['m3'] for f in fs)
    por = {}
    for f in fs:
        por[f['proveedor']] = por.get(f['proveedor'], 0.0) + f['m3']
    ypf = por.get('YPF', 0.0)
    sin = por.get('sin_declarar', 0.0)
    comp = {k: round(v, 1) for k, v in por.items()
            if k not in ('YPF', 'sin_declarar')}
    return {
        'm3_total': round(tot, 1),
        'm3_ypf_piso': round(ypf, 1),
        'm3_ypf_techo': round(ypf + sin, 1),
        # Cada competidor con su nombre, no un total anonimo.
        'm3_por_competidor': comp,
        'm3_sin_declarar': round(sin, 1),
        'rutas': len({(f['origin'], f['dest']) for f in fs}),
        'aerolineas': len({f['aerolinea'] for f in fs}),
        'vuelos': sum(f['vuelos'] for f in fs),
        # Que el rango sea un rango de verdad o el sintoma de que falta la lista es
        # una diferencia que el consumidor tiene que poder contar.
        # Con la planilla cargada el rango se cierra a las rutas competitivas no
        # declaradas; sin planilla, a todo AEP/EZE/COR.
        'share_confiable': est['clasificador'] == 'planilla',
        'estado': est,
    }


if __name__ == '__main__':
    if hasattr(sys.stdout, 'reconfigure'):
        sys.stdout.reconfigure(encoding='utf-8', errors='replace')

    if not disponible():
        raise SystemExit('No hay base de partidas en %s.\n'
                         'Corre el sondeo del radar primero.' % base_oficial())

    from app import app
    with app.app_context():
        r = resumen(horas=None)
    e = r['estado']
    print('partidas          : %d' % e['partidas'])
    print('  con matricula   : %d (%.0f%%)'
          % (e['con_matricula'], 100.0 * e['con_matricula'] / max(e['partidas'], 1)))
    print('  avion medido    : %d' % e['avion_medido'])
    print('  avion estimado  : %d' % e['avion_estimado'])
    print('  sin ruta        : %d' % e['sin_ruta'])
    print('  sin pax         : %d' % e['sin_pax'])
    print('  clasificador YPF: %s' % e['clasificador'])
    if e['clasificador'] == 'sin_planilla':
        print('      falta %s -- solo se resuelve el interior' % e['tabla_proveedores'])
    if e['iata_desconocidos']:
        print('  IATA sin mapear : %s' % e['iata_desconocidos'])
    if e['tipos_sin_flota']:
        print('  tipos sin flota : %s' % e['tipos_sin_flota'])
    print()
    print('rutas             : %d  | aerolineas: %d | vuelos: %d'
          % (r['rutas'], r['aerolineas'], r['vuelos']))
    print('m3 total          : %s' % r['m3_total'])
    print('m3 YPF            : %s a %s  (rango)' % (r['m3_ypf_piso'], r['m3_ypf_techo']))
    print('m3 competidores   : %s' % (r['m3_por_competidor'] or 'ninguno'))
    print('m3 sin declarar   : %s' % r['m3_sin_declarar'])
