# -*- coding: utf-8 -*-
r"""El formato de "BAJADA MAPA": todo lo que el web le manda al mapa de la terminal.

ESTE ARCHIVO ESTA EN LOS DOS REPOS Y TIENE QUE SER IDENTICO, igual que
intercambio_ms.py y por el mismo motivo: con un escritor y un lector separados, un dia
alguien agrega una tabla de un lado y el archivo pasa a significar cosas distintas segun
quien lo abrio. Ya paso con la planilla de proveedores.

QUE PROBLEMA RESUELVE
---------------------
El Excel de partidas (intercambio_ms.py) lleva 20 columnas de `vuelo_oficial` y NADA MAS.
No lleva el historico de ANAC ni la tabla de proveedores -- verificado: `proveedor` no
esta entre sus columnas. O sea que cambiar un proveedor en el web no llegaba nunca a la
terminal, y el historico de la terminal seguia siendo el del zip con que se instalo.

Esto lleva las tres cosas en un archivo:

    BAJADA MAPA.zip
      meta.json          formato, cuando se genero, hasta cuando llegan los datos
      datos.db           SQLite con las tablas del historico (lista blanca, abajo)
      proveedores.json   la distribucion de rutas por petrolera
      (vuelo_oficial va adentro de datos.db)

DOS REGLAS DISTINTAS PARA APLICARLO, Y LA DIFERENCIA IMPORTA
------------------------------------------------------------
**El historico y los proveedores se REEMPLAZAN.** El web es el duenio de esos datos: ahi
se suben las planillas de ANAC y ahi se edita la planilla de proveedores. La terminal es
un espejo, y un espejo que "completa" terminaria con una mezcla que no existe en ningun
lado -- una ruta borrada en el web seguiria viva en la terminal para siempre.

**Las partidas se COMPLETAN, nunca se reemplazan.** Es la regla contraria y no es una
inconsistencia: la hora real de un despegue vive unas horas en el feed de AA2000 y
despues desaparece, asi que la base de la terminal puede tener la UNICA copia de una hora
que el web nunca vio. Medido el 2026-09-08 entre las dos bases de esta PC: habia 224
horas de despegue que la otra no tenia. Reemplazar las partidas las perderia en silencio.
Por eso se aplican con intercambio_ms.importar(), que ya hace exactamente eso.

LA LISTA DE TABLAS ES BLANCA Y NO NEGRA
---------------------------------------
Y esto ya costo una pantalla en blanco una vez, en _meta_tabla() de quien_cargo.py: una
lista negra excluye lo que alguien penso en el momento de escribirla, y la tabla que se
agrega maniana entra sola sin que nadie lo decida. Aca el riesgo es peor -- que viaje
`fuel_sale` (ventas de YPF) o `app_user` a una carpeta compartida.

Ademas hay tablas que EXISTEN EN LOS DOS REPOS CON COLUMNAS DISTINTAS y por eso quedan
afuera a proposito (medido el 2026-09-10 comparando los dos models.py):

  manual_route      el local tiene vuelos_mes, pax_por_vuelo y las cuatro de vigencia;
                    el web no. Copiar la version del web las borraria.
  mercado_mensual   el local tiene la apertura CAB/INT por petrolera (ypf_cab_m3 y las
                    otras cinco) y el web no. Es el dato que sostiene el share por
                    mercado: perderlo devolveria el numero mezclado, que le erra por 12
                    puntos en cabotaje.
  presupuesto_mensual   existe SOLO en el local.
  app_user              existe SOLO en el web.

LAS VENTAS DE YPF SI VIAJAN, DESDE EL 2026-09-10 Y A PEDIDO
------------------------------------------------------------
`fuel_sale` estuvo prohibida y ahora esta en la lista. La objecion se planteo antes de
cambiarlo -- el archivo va a una biblioteca compartida con un equipo -- y la decision fue
que la terminal tiene que ver las ventas. Queda escrito para que no se lea como un
descuido.

Lo que NO cambia es `datos/semilla.db`: el zip que se reparte a mano las sigue vaciando
siempre y sin preguntar. Son dos paquetes con alcances distintos a proposito.

Siguen sin viajar `app_user` y `admin_file` (cuentas y archivos subidos, que el mapa de la
terminal no usa porque no tiene login) y los tres logs de carga, que son PROCEDENCIA y no
contenido.
"""
from __future__ import annotations

import io
import json
import os
import sqlite3
import tempfile
import time
import zipfile

FORMATO = 1

NOMBRE_ARCHIVO = 'BAJADA MAPA.zip'

INTERIOR_META = 'meta.json'
INTERIOR_DB = 'datos.db'
INTERIOR_PROVEEDORES = 'proveedores.json'

# LISTA BLANCA. Las tablas del historico que el web posee y la terminal espeja. Tienen
# que existir en los dos con las MISMAS columnas: ver el docstring.
TABLAS_ESPEJO = (
    'route_monthly',                  # el historico de ANAC: la tabla central
    'airport_monthly',
    'airline_monthly',
    'airline_load_factor_snapshot',
    'airport',
    'airport_alias',
    'aircraft',
    'indice_economico',
    'tipo_cambio_mensual',
    'proyeccion_config',
    'proyeccion_exclusion',
    'proyeccion_ruta',

    # LAS VENTAS DE YPF VIAJAN, POR DECISION EXPLICITA DEL 2026-09-10.
    #
    # Estuvieron prohibidas hasta hoy y el motivo sigue siendo cierto: el archivo se pega
    # en una biblioteca de SharePoint compartida con un equipo, y de ahi no se sabe donde
    # termina. Se planteo asi antes de cambiarlo y la respuesta fue que la terminal tiene
    # que ver las ventas. No es un olvido: si manana alguien se pregunta por que esto esta
    # aca, la respuesta es que se pidio sabiendo el costo.
    #
    # Sus 12 columnas son IDENTICAS en los dos repos (verificado contra los dos
    # models.py), asi que el espejo copia la tabla entera y no una parte.
    #
    # OJO CON LO QUE ESTO **NO** CAMBIA: `datos/semilla.db`, que viaja en el zip que se
    # reparte a mano, las sigue vaciando (actualizar_semilla.py, siempre y sin preguntar).
    # Los dos paquetes tienen alcances distintos a proposito -- el zip se manda por
    # cualquier lado y la bajada va a una biblioteca con permisos -- asi que una PC nueva
    # abre en cero y recibe las ventas con la primera bajada.
    'fuel_sale',
)

# Las partidas viajan en la misma base pero se aplican COMPLETANDO, no reemplazando.
TABLA_PARTIDAS = 'vuelo_oficial'

# Nunca viajan. No alcanza con que no esten en la lista blanca: se comprueba, porque el
# dia que alguien agregue una a la lista sin pensarlo el chequeo tiene que gritar.
#
# `fuel_sale` SALIO de esta tupla el 2026-09-10 -- ver el comentario en TABLAS_ESPEJO.
# Las tres que quedan no son negociables por otro motivo: `app_user` y `admin_file` son
# cuentas y archivos subidos, que no le sirven de nada al mapa de la terminal (no tiene
# login), y `fuel_sale_upload_log` es PROCEDENCIA y no contenido -- quien subio que
# planilla y cuando -- igual que `upload_log` y `airline_upload_log`, que tampoco viajan.
PROHIBIDAS = ('fuel_sale_upload_log', 'app_user', 'admin_file')

# LA TABLA SIN LA CUAL UNA BAJADA NO ES UNA BAJADA. Es el historico de ANAC: el mapa
# entero cuelga de ahi. Si viene vacia, quien la armo no tiene datos -- una instancia
# recien levantada, una base que no es la que la app usa -- y aplicarla borraria el
# historico de la terminal y pondria cero.
#
# Ese es el peor final posible de todo esto y no se ve como un error: se ve como un mapa
# sin rutas, y manda a buscar el problema a las planillas de ANAC. Medido el 2026-09-10
# armando la primera bajada desde la copia local del web: route_monthly vino con 0 filas
# porque los datos de verdad viven en el disco de Render, no en instance/.
IMPRESCINDIBLE = 'route_monthly'


def _columnas(con, tabla):
    """Las columnas que la tabla tiene DE VERDAD en esta base, no las que el ORM dice."""
    try:
        return [f[1] for f in con.execute('PRAGMA table_info("%s")' % tabla)]
    except sqlite3.Error:
        return []


def _columnas_attach(con, alias, tabla):
    """Igual, pero para una base ATTACHeada. PRAGMA no acepta alias."""
    try:
        return [f[1] for f in con.execute('PRAGMA "%s".table_info("%s")' % (alias, tabla))]
    except sqlite3.Error:
        return []


def _filas(con, tabla, columnas):
    sel = ', '.join('"%s"' % c for c in columnas)
    return con.execute('SELECT %s FROM "%s"' % (sel, tabla))


def escribir(con_origen, proveedores=None, con_partidas=None, destino=None, meta=None):
    """Arma el zip. `destino` es una ruta, o None para devolver los bytes.

    `con_origen` es la base del web (sqlite3.Connection). `con_partidas` es la de
    aa2000_oficial, que es otro archivo. `proveedores` es el dict de la planilla.
    """
    tmp = tempfile.mkdtemp(prefix='bajada-')
    ruta_db = os.path.join(tmp, INTERIOR_DB)
    conteos = {}
    try:
        dest = sqlite3.connect(ruta_db)
        try:
            for tabla in TABLAS_ESPEJO:
                cols = _columnas(con_origen, tabla)
                if not cols:
                    # Que falte una tabla no puede abortar la bajada entera: el web puede
                    # estar en una version anterior. Se anota como ausente y el que aplica
                    # NO la toca -- que es muy distinto de vaciarla.
                    conteos[tabla] = None
                    continue
                dest.execute('CREATE TABLE "%s" (%s)'
                             % (tabla, ', '.join('"%s"' % c for c in cols)))
                n = 0
                for fila in _filas(con_origen, tabla, cols):
                    dest.execute('INSERT INTO "%s" VALUES (%s)'
                                 % (tabla, ', '.join('?' * len(cols))), fila)
                    n += 1
                conteos[tabla] = n

            if con_partidas is not None:
                cols = _columnas(con_partidas, TABLA_PARTIDAS)
                if cols:
                    dest.execute('CREATE TABLE "%s" (%s)'
                                 % (TABLA_PARTIDAS, ', '.join('"%s"' % c for c in cols)))
                    n = 0
                    for fila in _filas(con_partidas, TABLA_PARTIDAS, cols):
                        dest.execute('INSERT INTO "%s" VALUES (%s)'
                                     % (TABLA_PARTIDAS, ', '.join('?' * len(cols))), fila)
                        n += 1
                    conteos[TABLA_PARTIDAS] = n
            dest.commit()
        finally:
            dest.close()

        # SE FALLA ACA, DONDE HAY ALGUIEN MIRANDO. El que baja el archivo ve el error y
        # entiende que su instancia no tiene datos; si la dejaramos salir, el que se
        # entera es el de la terminal, tres dias despues y con el historico ya borrado.
        if not conteos.get(IMPRESCINDIBLE):
            raise ValueError(
                'la bajada saldria con %s vacia (%s filas): esta base no tiene el '
                'historico. Aplicarla borraria el de la terminal.'
                % (IMPRESCINDIBLE, conteos.get(IMPRESCINDIBLE)))

        m = dict(meta or {})
        m.update({
            'formato': FORMATO,
            'generado': time.strftime('%Y-%m-%dT%H:%M:%S'),
            'filas': conteos,
            'tablas_espejo': list(TABLAS_ESPEJO),
        })

        buf = io.BytesIO()
        with zipfile.ZipFile(buf, 'w', zipfile.ZIP_DEFLATED) as z:
            z.writestr(INTERIOR_META, json.dumps(m, ensure_ascii=False, indent=2))
            z.write(ruta_db, INTERIOR_DB)
            if proveedores is not None:
                z.writestr(INTERIOR_PROVEEDORES,
                           json.dumps(proveedores, ensure_ascii=False, indent=2))
        datos = buf.getvalue()
    finally:
        try:
            os.remove(ruta_db)
            os.rmdir(tmp)
        except OSError:
            pass

    if destino:
        with open(destino, 'wb') as f:
            f.write(datos)
        return None
    return datos


def leer(ruta):
    """Abre el zip y devuelve (meta, ruta_db_temporal, proveedores).

    El .db se extrae a un temporal porque sqlite necesita un archivo real. El que llama
    tiene que borrarlo -- aplicar() lo hace.
    """
    with zipfile.ZipFile(ruta) as z:
        nombres = set(z.namelist())
        if INTERIOR_META not in nombres or INTERIOR_DB not in nombres:
            raise ValueError('el zip no es una bajada del mapa: le falta %s o %s'
                             % (INTERIOR_META, INTERIOR_DB))
        meta = json.loads(z.read(INTERIOR_META).decode('utf-8-sig'))
        if int(meta.get('formato') or 0) != FORMATO:
            raise ValueError('bajada en formato %s, este mapa lee el %d'
                             % (meta.get('formato'), FORMATO))
        tmp = tempfile.mkdtemp(prefix='bajada-')
        ruta_db = os.path.join(tmp, INTERIOR_DB)
        with open(ruta_db, 'wb') as f:
            f.write(z.read(INTERIOR_DB))
        prov = None
        if INTERIOR_PROVEEDORES in nombres:
            prov = json.loads(z.read(INTERIOR_PROVEEDORES).decode('utf-8-sig'))
    return meta, ruta_db, prov


def espejar(con_destino, ruta_db, log=None):
    """Reemplaza las tablas de la lista blanca. Todo o nada.

    LAS COLUMNAS SE CRUZAN POR NOMBRE, nunca por posicion -- misma regla que el Excel. Si
    un lado tiene una columna que el otro no, se saltea; copiando por posicion, una
    columna de mas corre todas las demas y no falla nada, que es la peor forma de fallar.
    """
    for t in TABLAS_ESPEJO:
        if t in PROHIBIDAS:                              # cinturon y tiradores
            raise ValueError('%s no puede estar en la lista blanca' % t)

    hecho = {}
    con_destino.execute('ATTACH DATABASE ? AS bajada', (ruta_db,))
    try:
        # UNA SOLA TRANSACCION: si algo revienta a la mitad, la base queda como estaba y no
        # a medio reemplazar. Una base a medias no se ve como un error -- se ve como un
        # mapa al que le faltan rutas, y manda a buscar el problema a los datos de origen.
        con_destino.execute('BEGIN')
        for tabla in TABLAS_ESPEJO:
            origen = _columnas_attach(con_destino, 'bajada', tabla)
            if not origen:
                hecho[tabla] = None                      # no vino: no se toca
                continue
            destino = _columnas(con_destino, tabla)
            comunes = [c for c in origen if c in destino]
            if not comunes:
                hecho[tabla] = None
                continue
            # UNA TABLA QUE LLEGA VACIA NO BORRA UNA QUE TIENE FILAS. El cinturon de
            # arriba (IMPRESCINDIBLE) cubre el caso grave en el origen, pero esto es lo
            # que protege a la terminal de una bajada armada por una version anterior, o
            # de una tabla que el web todavia no llena. Se salta y se dice; vaciarla en
            # silencio se veria como datos que faltan, no como un archivo incompleto.
            n_origen = con_destino.execute(
                'SELECT COUNT(*) FROM bajada."%s"' % tabla).fetchone()[0]
            n_destino = con_destino.execute(
                'SELECT COUNT(*) FROM "%s"' % tabla).fetchone()[0]
            if n_origen == 0 and n_destino > 0:
                hecho[tabla] = None
                if log:
                    log('%s: la bajada la trae vacia y aca hay %d filas; NO se toca'
                        % (tabla, n_destino))
                continue

            sel = ', '.join('"%s"' % c for c in comunes)
            con_destino.execute('DELETE FROM "%s"' % tabla)
            con_destino.execute('INSERT INTO "%s" (%s) SELECT %s FROM bajada."%s"'
                                % (tabla, sel, sel, tabla))
            hecho[tabla] = con_destino.execute(
                'SELECT COUNT(*) FROM "%s"' % tabla).fetchone()[0]
            if log and len(comunes) != len(destino):
                log('%s: %d columnas en comun de %d que tiene la base'
                    % (tabla, len(comunes), len(destino)))
        con_destino.execute('COMMIT')
    except Exception:
        try:
            con_destino.execute('ROLLBACK')
        except sqlite3.Error:
            pass
        raise
    finally:
        try:
            con_destino.execute('DETACH DATABASE bajada')
        except sqlite3.Error:
            pass
    return hecho


def partidas_de(ruta_db):
    """Las filas de vuelo_oficial del zip, como dicts. [] si no vinieron.

    Como dicts y no como tuplas para que las coma intercambio_ms.importar(), que es el que
    sabe COMPLETAR sin pisar. Esa logica no se duplica aca.
    """
    con = sqlite3.connect(ruta_db)
    try:
        cols = _columnas(con, TABLA_PARTIDAS)
        if not cols:
            return []
        sel = ', '.join('"%s"' % c for c in cols)
        return [dict(zip(cols, f))
                for f in con.execute('SELECT %s FROM "%s"' % (sel, TABLA_PARTIDAS))]
    finally:
        con.close()


def guardar_proveedores(prov, destino):
    """Escribe la planilla de forma atomica: se escribe al lado y se renombra.

    Si el proceso muere a mitad de un write directo, `proveedores.json` queda truncado y
    el clasificador cae a "sin declarar" para TODAS las rutas -- sin un error, con el
    market share en cero y sin decir que lo que falta es la planilla.
    """
    parcial = destino + '.parcial'
    with io.open(parcial, 'w', encoding='utf-8') as f:
        json.dump(prov, f, ensure_ascii=False, indent=2)
    os.replace(parcial, destino)
    return destino
