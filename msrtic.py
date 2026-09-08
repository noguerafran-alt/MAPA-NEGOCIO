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
#   - aircraft_db.sqlite pesa 52 MB y se construye una vez desde OpenSky. Un repo que
#                        despliega no puede cargar 52 MB de blob, y el disco ya existe.
#
# Que esten en el disco y no en el repo es lo que hace que el acumulado sobreviva a los
# deploys: el filesystem de Render es efimero salvo el disco montado.
DISCO = os.environ.get('RENDER_DISK_PATH', '/var/data')


def base_oficial():
    return os.environ.get('MS_RTIC_OFICIAL') or os.path.join(DISCO, 'aa2000_oficial.db')


def base_aviones():
    return os.environ.get('MS_RTIC_AVIONES') or os.path.join(DISCO, 'aircraft_db.sqlite')


def tabla_proveedores():
    # Este SI va en el repo, y es el unico dato no publico de todo el modulo: dice que
    # rutas abastece cada petrolera. Por eso /api/msrtic exige nivel 1 -- los usuarios
    # los crea el admin, asi que detras del login solo hay gente de YPF.
    return os.environ.get('MS_RTIC_PROVEEDORES') or os.path.join(
        BASE, 'datos', 'proveedores.json')


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


def _tipos_por_matricula(matriculas):
    """{matricula normalizada: typecode} para las que esten en el registro.

    Una sola pasada con todas las matriculas del periodo, no una consulta por
    fila: son ~600.000 aeronaves y abrir la base por vuelo cuesta mas que todo el
    resto junto.
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


def calcular(horas=24.0, coords=None):
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

    crudas = _partidas(horas)
    coords = _coords() if coords is None else coords

    mats = {_norm_matricula(p.get('matricula')) for p in crudas}
    mats.discard('')
    tipos = _tipos_por_matricula(mats)
    flota = avion_model.get_flota()

    est = {'partidas': len(crudas), 'con_matricula': 0, 'tipo_resuelto': 0,
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
                m3 = tons / DENSIDAD_T_M3
        if m3 is None:
            est['sin_consumo'] += 1

        # QUIEN ABASTECE ES PROPIEDAD DE LA RUTA, NO DE LA AEROLINEA. La planilla de
        # YPF esta armada por ruta dirigida: si dos aerolineas hacen AEP-BRC, las dos
        # cargan con el mismo proveedor. Por eso el desagregado por aerolinea sirve
        # para ver el CONSUMO de cada una, no para separar proveedores adentro de una
        # ruta -- ahi el proveedor es uno solo.
        proveedor, motivo = None, 'sin_clasificador'
        if ms is not None and lista is not None:
            try:
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
        f = acum.setdefault(clave, {'vuelos': 0, 'pax': 0, 'con_pax': 0, 'm3': 0.0,
                                    'medidos': 0, 'estimados': 0, 'aviones': {}})
        f['vuelos'] += 1
        if pax is not None:
            f['pax'] += pax
            f['con_pax'] += 1
        if m3 is not None:
            f['m3'] += m3
        f['medidos' if medido else 'estimados'] += 1
        if codigo:
            f['aviones'][codigo] = f['aviones'].get(codigo, 0) + 1

    filas_out = []
    for (tipo, o, d, aero, aero_id, bandera, motivo), v in sorted(acum.items()):
        filas_out.append({
            'tipo': tipo, 'origin': o, 'dest': d,
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
            'm3': round(v['m3'], 1),
            'aviones_medidos': v['medidos'],
            'aviones_estimados': v['estimados'],
            'aviones': sorted(v['aviones'], key=lambda c: -v['aviones'][c]),
        })
        cr = coords_ruta.get((o, d))
        if cr:
            filas_out[-1].update(olat=cr[0], olon=cr[1], dlat=cr[2], dlon=cr[3],
                                 distancia_km=round(cr[4], 1))
    return filas_out, est


def _clave_de_archivo():
    """Mtime y tamano de la base: cambia cuando el radar sondea de nuevo."""
    try:
        st = os.stat(base_oficial())
        return (st.st_mtime_ns, st.st_size)
    except OSError:
        return None


def filas(horas=24.0, usar_cache=True):
    """Las filas del filtro, cacheadas hasta que el radar vuelva a escribir."""
    clave = (_clave_de_archivo(), horas)
    with _lock:
        if usar_cache and _cache['clave'] == clave and _cache['filas'] is not None:
            return _cache['filas'], _cache['estado']
        fs, est = calcular(horas)
        _cache['clave'], _cache['filas'], _cache['estado'] = clave, fs, est
        return fs, est


def resumen(horas=24.0):
    """Lo que necesita el banner del mapa: totales y el rango de YPF.

    El share va como RANGO. Con la lista de clientes no exhaustiva, el piso son los
    m3 de los clientes confirmados y el techo suma los sin clasificar; dar un numero
    solo seria elegir uno de los dos sin decirlo.
    """
    fs, est = filas(horas)
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
