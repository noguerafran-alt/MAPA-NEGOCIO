# -*- coding: utf-8 -*-
"""Panel unificado ruta-mes para las proyecciones estadisticas.

Junta las dos fuentes que tiene la app y las deja en una sola tabla larga:

  - historical_2001_2022.json  -> 2001-01 a 2022-12, del scrapeo viejo de ANAC.
  - tabla route_monthly        -> 2023-01 en adelante, lo que se carga por /admin.

Hay dos trampas en el empalme, y las dos estan resueltas aca adentro para que
ningun modelo las tenga que volver a descubrir:

  1. UNIDADES. En el JSON los pasajeros vienen en MILES (Aeroparque-Cordoba,
     dic-2022 = 97.825 son 97.825 pasajeros) mientras que route_monthly los
     guarda en unidades (112.643 para esa misma ruta en nov-2023). Los vuelos,
     en cambio, estan en unidades en las dos fuentes. Sin corregir, el empalme
     mete un salto de x1000 justo en el corte 2022/2023 y cualquier modelo
     aprende esa discontinuidad como si fuera demanda.

  2. LA FILA "Otros". El JSON trae una serie agregada llamada "Otros" que suma
     todo lo que no entro en las rutas nominadas. No es una ruta: se excluye,
     porque si no compite con las rutas reales dentro del modelo global.

Los pasajeros nulos (~18% de las filas nuevas: ANAC informa vuelos pero no
pasajeros en algunas rutas) se dejan como NaN, no como cero. Un cero seria
mentira y arrastraria la proyeccion a la baja.
"""

import json
import os
import sqlite3

import numpy as np
import pandas as pd

BASE = os.path.dirname(os.path.abspath(__file__))
JSON_HISTORICO = os.path.join(BASE, 'historical_2001_2022.json')

# La base no siempre es la misma: en Render es el SQLite del disco persistente
# (/var/data/mapa.db), en local el de instance/, y podria volver a ser Postgres.
# app.py llama a usar_base() con la URL que realmente configuro, para que la
# proyeccion lea la misma base que el mapa y no una copia vieja al lado.
DB_URL = None

MESES = {'Ene': 1, 'Feb': 2, 'Mar': 3, 'Abr': 4, 'May': 5, 'Jun': 6,
         'Jul': 7, 'Ago': 8, 'Sep': 9, 'Oct': 10, 'Nov': 11, 'Dic': 12}

ANIO_BASE = 2001  # origen del indice temporal t

# Bloques del JSON -> (tipo, metrica, factor de escala a unidades)
BLOQUES_JSON = {
    'cabotaje_pax':     ('cabotaje',      'pax',    1000.0),
    'intl_pax':         ('internacional', 'pax',    1000.0),
    'cabotaje_vuelos':  ('cabotaje',      'vuelos',    1.0),
    'intl_vuelos':      ('internacional', 'vuelos',    1.0),
}

NO_ES_RUTA = {'Otros', 'otros', 'OTROS'}


def _t(anio, mes):
    """Indice temporal continuo en meses desde enero de ANIO_BASE."""
    return (int(anio) - ANIO_BASE) * 12 + (int(mes) - 1)


def _desde_json(path=JSON_HISTORICO):
    with open(path, encoding='utf-8') as fh:
        crudo = json.load(fh)

    filas = []
    for bloque, (tipo, metrica, escala) in BLOQUES_JSON.items():
        for ruta, serie in crudo.get(bloque, {}).items():
            if ruta in NO_ES_RUTA:
                continue
            for periodo, valor in serie.items():
                anio, mes_txt = periodo.split('-')
                if mes_txt not in MESES:
                    continue
                filas.append((tipo, ruta, int(anio), MESES[mes_txt], metrica,
                              None if valor is None else float(valor) * escala))

    df = pd.DataFrame(filas, columns=['tipo', 'ruta', 'anio', 'mes', 'metrica', 'valor'])
    # pax y vuelos vienen en bloques separados: los cruzo a columnas.
    df = df.pivot_table(index=['tipo', 'ruta', 'anio', 'mes'],
                        columns='metrica', values='valor', aggfunc='first').reset_index()
    df.columns.name = None
    for col in ('pax', 'vuelos'):
        if col not in df.columns:
            df[col] = np.nan
    df['fuente'] = 'json'
    return df[['tipo', 'ruta', 'anio', 'mes', 'pax', 'vuelos', 'fuente']]


def usar_base(url):
    """Fija la base que va a leer la proyeccion. La llama app.py al arrancar."""
    global DB_URL
    DB_URL = (url or '').strip() or None


def _url_por_defecto():
    """Misma busqueda que hace app.py, para poder correr este modulo a mano."""
    disco = os.environ.get('RENDER_DISK_PATH', '/var/data')
    if os.path.isdir(disco) and os.access(disco, os.W_OK):
        return 'sqlite:///' + os.path.join(disco, 'mapa.db')
    env = (os.environ.get('DATABASE_URL') or '').strip()
    if env:
        return env.replace('postgres://', 'postgresql://', 1)
    for cand in (os.path.join(BASE, 'instance', 'local_dev.db'),
                 os.path.join(BASE, 'instance', 'mapa.db'),
                 os.path.join(BASE, 'local_dev.db')):
        if os.path.exists(cand):
            return 'sqlite:///' + cand
    return None


def _ruta_sqlite(url):
    """Path del archivo en una URL sqlite. Los relativos se buscan igual que los
    resuelve Flask-SQLAlchemy: primero instance/, despues la raiz del proyecto."""
    path = url.split('sqlite:///', 1)[-1]
    if os.path.isabs(path):
        return path
    for cand in (os.path.join(BASE, 'instance', path), os.path.join(BASE, path)):
        if os.path.exists(cand):
            return cand
    return os.path.join(BASE, 'instance', path)


SQL_RUTAS = ('SELECT tipo, origin, dest, year AS anio, month AS mes, vuelos, pax '
             'FROM route_monthly')


def _desde_db(url=None):
    vacio = pd.DataFrame(columns=['tipo', 'ruta', 'anio', 'mes', 'pax', 'vuelos', 'fuente'])
    url = url or DB_URL or _url_por_defecto()
    if not url:
        return vacio

    if url.startswith('sqlite'):
        path = _ruta_sqlite(url)
        if not os.path.exists(path):
            return vacio
        con = sqlite3.connect(path)
        try:
            df = pd.read_sql_query(SQL_RUTAS, con)
        finally:
            con.close()
    else:
        # Postgres u otro motor: se delega en SQLAlchemy, que ya es dependencia
        # de la app por Flask-SQLAlchemy.
        from sqlalchemy import create_engine
        motor = create_engine(url)
        try:
            df = pd.read_sql_query(SQL_RUTAS, motor)
        finally:
            motor.dispose()

    if df.empty:
        return vacio

    df['ruta'] = df['origin'].astype(str) + ' - ' + df['dest'].astype(str)
    df['anio'] = df['anio'].astype(int)
    df['mes'] = df['mes'].map(MESES)
    df = df.dropna(subset=['mes'])
    df['mes'] = df['mes'].astype(int)
    df = df[~df['ruta'].isin(NO_ES_RUTA)]
    df['fuente'] = 'db'
    return df[['tipo', 'ruta', 'anio', 'mes', 'pax', 'vuelos', 'fuente']]


def cargar_panel(rellenar_huecos=True, db_url=None):
    """Devuelve el panel largo ruta-mes con pax y vuelos en unidades.

    Si `rellenar_huecos`, completa con NaN los meses faltantes entre el primer y
    el ultimo mes con dato de cada ruta, para que los lags no salteen periodos
    (un lag de 12 filas tiene que ser un lag de 12 MESES).
    """
    df = pd.concat([_desde_json(), _desde_db(db_url)], ignore_index=True)

    # Si una ruta-mes aparece en las dos fuentes, mando la de la base: es la
    # carga mas reciente hecha por el usuario desde /admin.
    df['prio'] = (df['fuente'] == 'db').astype(int)
    df = (df.sort_values('prio')
            .drop_duplicates(subset=['tipo', 'ruta', 'anio', 'mes'], keep='last')
            .drop(columns='prio'))

    df['t'] = [_t(a, m) for a, m in zip(df['anio'], df['mes'])]
    df = df.sort_values(['tipo', 'ruta', 't']).reset_index(drop=True)

    # En el JSON las rutas inexistentes vienen rellenadas con ceros hasta el mes
    # en que nacieron. Un cero antes del primer vuelo real no es "cero demanda",
    # es "no existia": lo paso a NaN para no ensuciar el arranque de cada serie.
    for col in ('pax', 'vuelos'):
        nacio = df.groupby(['tipo', 'ruta'])[col].transform(
            lambda s: (s.fillna(0) > 0).cummax())
        df.loc[~nacio.astype(bool), col] = np.nan

    if rellenar_huecos:
        df = _rellenar(df)

    return df.sort_values(['tipo', 'ruta', 't']).reset_index(drop=True)


def _rellenar(df):
    partes = []
    for (tipo, ruta), g in df.groupby(['tipo', 'ruta'], sort=False):
        g = g.set_index('t')
        completo = pd.RangeIndex(g.index.min(), g.index.max() + 1, name='t')
        g = g.reindex(completo)
        g['tipo'], g['ruta'] = tipo, ruta
        g['anio'] = ANIO_BASE + (g.index // 12)
        g['mes'] = (g.index % 12) + 1
        partes.append(g.reset_index())
    return pd.concat(partes, ignore_index=True)


def ultimo_periodo(panel):
    """(anio, mes, t) del ultimo mes con algun dato cargado."""
    con_dato = panel[panel['pax'].notna() | panel['vuelos'].notna()]
    t = int(con_dato['t'].max())
    return ANIO_BASE + t // 12, (t % 12) + 1, t


if __name__ == '__main__':
    import sys
    if hasattr(sys.stdout, 'reconfigure'):
        sys.stdout.reconfigure(encoding='utf-8', errors='replace')

    panel = cargar_panel()
    anio, mes, _ = ultimo_periodo(panel)
    print('filas ruta-mes :', len(panel))
    print('rutas          :', panel.groupby(['tipo', 'ruta']).ngroups)
    print('ultimo periodo :', anio, mes)
    print('pax no nulos   :', int(panel['pax'].notna().sum()))
    print('vuelos no nulos:', int(panel['vuelos'].notna().sum()))
    print()
    print('control de empalme 2022/2023 (pax mensuales promedio, en unidades):')
    for ruta in ['Aeroparque - Córdoba', 'Aeroparque - Mendoza']:
        g = panel[(panel['ruta'] == ruta) & (panel['tipo'] == 'cabotaje')]
        fila = []
        for a in (2019, 2022, 2023, 2025):
            v = g[g['anio'] == a]['pax'].mean()
            fila.append('%d=%s' % (a, 'sin dato' if pd.isna(v) else '%.0f' % v))
        print('   %-22s %s' % (ruta, '  '.join(fila)))
