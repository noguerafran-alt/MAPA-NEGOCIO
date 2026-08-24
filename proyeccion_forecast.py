# -*- coding: utf-8 -*-
"""Proyeccion de vuelos y pasajeros por ruta, en el formato que consume el mapa.

La idea de fondo: el mapa ya sabe convertir vuelos+pasajeros en combustible. En
/api/data cada ruta viaja con su avion resuelto por ocupacion y su consumo por
vuelo (consumo_total_kg / consumo_total_m3), y render() en map.html multiplica
por los vuelos del periodo elegido. O sea que para "ver a futuro que se va a
vender en cada ruta" NO hace falta proyectar combustible: alcanza con proyectar
vuelos y pasajeros, meterlos como un periodo mas, y la cadena de consumo que ya
existe hace el resto sola.

Por eso este modulo devuelve filas con la misma forma que las de RouteMonthly:
    {'tipo', 'origin', 'dest', 'year', 'month', 'vuelos', 'pax', 'proyectado'}

QUE SE PROYECTA Y QUE NO
------------------------
- Solo rutas ACTIVAS: con al menos un mes de vuelos en el ultimo ano. Sin este
  filtro el modelo revive rutas muertas -- las de El Palomar, por ejemplo, que
  dejaron de operar en 2020 -- y el mapa dibujaria trafico que no va a existir.
- Los pasajeros salen de su propio modelo, no de vuelos x ocupacion fija. En
  ~18% de las filas ANAC informa vuelos pero no pasajeros; ahi pax queda en
  None, que es lo que el mapa ya sabe manejar.
- El horizonte es 12 meses. No es arbitrario: el modelo ancla cada prediccion
  en el mismo mes del ano anterior, asi que a partir del mes 13 el ancla seria
  una prediccion propia y el error se realimentaria.

PRECISION ESPERADA (del backtest en proyeccion_modelo.py, ventanas 2024-2026)
    vuelos  MASE 0.63-0.68  contra 0.77-0.79 del naive estacional
    pax     MASE 0.98-1.02  contra 1.01-1.08
Es una mejora real pero moderada. Un MASE cerca de 1 significa que el error
tipico a 12 meses es del orden del que comete predecir "lo mismo que el ano
pasado": sirve para dimensionar, no para comprometer volumen contractual.
"""

import threading

import numpy as np
import pandas as pd

from proyeccion_datos import ANIO_BASE, cargar_panel, usar_base  # noqa: F401
from proyeccion_modelo import (CATEGORICAS, COVID, HORIZONTES, PARAMS_LGBM,
                               _matriz_serie, _preparar, construir_muestras)

MESES_TXT = ['Ene', 'Feb', 'Mar', 'Abr', 'May', 'Jun',
             'Jul', 'Ago', 'Sep', 'Oct', 'Nov', 'Dic']

MESES_ACTIVIDAD = 12    # ventana para considerar una ruta viva
_cache = {'clave': None, 'filas': None}
_lock = threading.Lock()


def _entrenar_y_predecir(mat, origen, horizontes):
    """Entrena con todo lo disponible hasta `origen` y predice los h siguientes."""
    import lightgbm as lgb

    entren = []
    for T2 in range(origen - 12 * 20, origen - 11, 3):
        if T2 - 24 < int(mat.columns.min()):
            continue
        m = construir_muestras(mat, T2)
        m = m[m['t_obj'] <= origen]
        if len(m):
            entren.append(m)
    if not entren:
        return None

    entren = pd.concat(entren, ignore_index=True)
    entren = entren[~entren['t_obj'].between(*COVID)]

    futuro = construir_muestras(mat, origen, horizontes=horizontes, con_target=False)
    if not len(entren) or not len(futuro):
        return None

    cats = {c: pd.CategoricalDtype(
                sorted(set(entren[c]) | set(futuro[c]))).categories
            for c in CATEGORICAS}

    modelo = lgb.LGBMRegressor(**PARAMS_LGBM)
    modelo.fit(_preparar(entren, cats), entren['target'],
               categorical_feature=CATEGORICAS)

    crudo = (futuro['ancla'] + 1) * np.exp(
        modelo.predict(_preparar(futuro, cats))) - 1

    # Misma regla de seguridad que en el backtest: sin referencia interanual
    # valida no se corrige el naive estacional.
    a_ciegas = futuro['r_12m'].isna() & futuro['r_ult'].isna()
    futuro = futuro.assign(pred=np.where(a_ciegas, futuro['ancla'], crudo))
    futuro['pred'] = futuro['pred'].clip(lower=0)
    return futuro[['tipo', 'ruta', 't_obj', 'pred']]


def _rutas_activas(panel, t_max):
    reciente = panel[(panel['t'] > t_max - MESES_ACTIVIDAD)
                     & (panel['vuelos'].fillna(0) > 0)]
    return set(map(tuple, reciente[['tipo', 'ruta']].drop_duplicates().values))


def proyectar(panel=None, meses=12):
    """DataFrame con tipo, ruta, anio, mes, vuelos, pax proyectados."""
    panel = cargar_panel() if panel is None else panel
    con_dato = panel[panel['pax'].notna() | panel['vuelos'].notna()]
    t_max = int(con_dato['t'].max())
    horizontes = [h for h in HORIZONTES if h <= meses]

    activas = _rutas_activas(panel, t_max)
    salida = {}
    for metrica in ('vuelos', 'pax'):
        pred = _entrenar_y_predecir(_matriz_serie(panel, metrica), t_max, horizontes)
        if pred is None:
            continue
        pred = pred[[tuple(k) in activas for k in pred[['tipo', 'ruta']].values]]
        salida[metrica] = pred.rename(columns={'pred': metrica})

    if 'vuelos' not in salida:
        return pd.DataFrame(columns=['tipo', 'ruta', 'anio', 'mes', 'vuelos', 'pax'])

    df = salida['vuelos']
    if 'pax' in salida:
        df = df.merge(salida['pax'], on=['tipo', 'ruta', 't_obj'], how='left')
    else:
        df['pax'] = np.nan

    df['anio'] = ANIO_BASE + df['t_obj'] // 12
    df['mes'] = (df['t_obj'] % 12) + 1
    df['vuelos'] = df['vuelos'].round().astype(int)
    df['pax'] = df['pax'].round()

    # Una ruta con cero vuelos proyectados no se dibuja: es ruido, no informacion.
    return df[df['vuelos'] > 0].reset_index(drop=True)


def rutas_proyectadas(panel=None, meses=12, usar_cache=True):
    """Filas listas para sumar a `all_routes` en /api/data.

    Cachea en memoria: entrenar los dos modelos toma varios segundos y /api/data
    se llama en cada carga del mapa. La clave incluye el ultimo periodo y el
    total de filas con dato, asi que subir una planilla nueva por /admin invalida
    el cache sin que haya que acordarse de limpiarlo a mano.
    """
    panel = cargar_panel() if panel is None else panel
    con_dato = panel[panel['pax'].notna() | panel['vuelos'].notna()]
    clave = (int(con_dato['t'].max()), len(con_dato), meses)

    with _lock:
        if usar_cache and _cache['clave'] == clave:
            return _cache['filas']

        df = proyectar(panel, meses=meses)
        filas = []
        for r in df.itertuples(index=False):
            origin, dest = r.ruta.split(' - ', 1)
            filas.append({
                'tipo': r.tipo, 'origin': origin, 'dest': dest,
                'year': str(int(r.anio)), 'month': MESES_TXT[int(r.mes) - 1],
                'vuelos': int(r.vuelos),
                'pax': None if pd.isna(r.pax) else int(r.pax),
                'proyectado': True,
            })

        _cache['clave'], _cache['filas'] = clave, filas
        return filas


def periodos_proyectados(filas):
    """Claves 'AAAA-Mmm' que son proyeccion, para que el frontend las marque."""
    return sorted({'%s-%s' % (f['year'], f['month']) for f in filas})


if __name__ == '__main__':
    import sys
    import time
    if hasattr(sys.stdout, 'reconfigure'):
        sys.stdout.reconfigure(encoding='utf-8', errors='replace')

    t0 = time.time()
    panel = cargar_panel()
    filas = rutas_proyectadas(panel)
    print('filas proyectadas : %d  (%.1f s)' % (len(filas), time.time() - t0))
    print('periodos          : %s' % ', '.join(periodos_proyectados(filas)))
    print('rutas             : %d' % len({(f['origin'], f['dest']) for f in filas}))
    print('sin pax           : %d' % sum(1 for f in filas if f['pax'] is None))
    print()
    print('muestra (primer mes proyectado):')
    primero = periodos_proyectados(filas)[0]
    m = [f for f in filas if '%s-%s' % (f['year'], f['month']) == primero]
    m.sort(key=lambda f: -(f['pax'] or 0))
    for f in m[:8]:
        print('   %-34s vuelos=%4d  pax=%s'
              % (f['origin'] + ' - ' + f['dest'], f['vuelos'],
                 'N/D' if f['pax'] is None else '%6d' % f['pax']))
