# -*- coding: utf-8 -*-
r"""Proyeccion con TimesFM, para precomputar el mismo JSON que hoy hace LightGBM.

Este modulo NO lo importa el servidor. Render sirve `proyeccion_precomputada.json`
a traves de `proyeccion_archivo.py`, que no importa nada fuera de la stdlib; aca
solo se elige QUIEN calcula ese archivo antes de commitearlo.

Por que TimesFM: le gana a LightGBM por 13,1% de MASE en pasajeros y 18,2% en
vuelos sobre las mismas 18 ventanas, corriendo zero-shot -- no vio un solo dato
argentino. Y gana justo donde el modelo del repo sufre: en 2022, la salida de la
pandemia, LightGBM da 1.84 (identico al naive, porque su regla de seguridad lo
hace caer al naive sin referencia interanual valida) y TimesFM da 0.85.

Que no cambia nada del deploy: el JSON tiene la misma forma, las mismas filas y
el mismo consumidor. La instancia de 512 MB de Render nunca ve torch, igual que
hoy no ve lightgbm.

COMO SE CORRE

No con el Python del repo -- ese no tiene torch y no debe tenerlo:

    %USERPROFILE%\venvs\timesfm-backtest\Scripts\python.exe precomputar_proyeccion.py \
        --modelo timesfm --db ..\MAPA-NEGOCIO-LOCAL\instance\local_dev.db

UNA COSA QUE NO SE PUEDE CAMBIAR SIN VOLVER A MEDIR

Los NaN del principio de cada serie son "la ruta no existia todavia" y se
recortan; los NaN INTERNOS se INTERPOLAN, nunca se borran. Sacarlos compacta la
serie y le corre la estacionalidad, y en este panel la mitad de las series tienen
NaN internos, asi que un enero puede terminar alineado contra un marzo. Costo 5
puntos de MASE cuando paso en el backtest, y el numero malo era creible.

El reverso de esa moneda esta medido y sin resolver: en una ruta ESTACIONAL
(Bariloche-San Pablo vuela en temporada de ski y el resto del ano no aparece en
la planilla) los NaN internos no son "sin dato" sino "no opero", y al
interpolarlos el mes muerto se infla. En el mapa local son 21 filas de 2.623
(0,8%), todas chicas. Cambiar el criterio obliga a correr el backtest entero de
nuevo: ver ESTADO.md del repo local.
"""
import sys
import time

import numpy as np

from proyeccion_datos import ANIO_BASE, cargar_panel
from proyeccion_forecast import MESES_ACTIVIDAD, MESES_TXT
from proyeccion_modelo import _matriz_serie

HORIZONTE = 12

# Minimo de meses de historia para pedirle algo a TimesFM. Es el mismo umbral que
# uso el backtest, y tiene que seguir siendolo: si aca se proyectaran rutas que el
# backtest nunca midio, el MASE reportado dejaria de describir lo que se publica.
MIN_CONTEXTO = 24

CHECKPOINT = 'google/timesfm-3.0-pytorch'


def _contexto(valores):
    """La serie lista para TimesFM, o None si no alcanza el historial."""
    # np.array y no np.asarray: pandas 3 devuelve vistas de solo lectura y la
    # interpolacion escribe sobre el array. Con asarray revienta con "assignment
    # destination is read-only", y solo en las series que tienen NaN internos --
    # o sea a mitad de la corrida, no al principio.
    hist = np.array(valores, dtype=float)
    con_dato = np.where(~np.isnan(hist))[0]
    if len(con_dato) == 0:
        return None
    hist = hist[con_dato[0]:]
    if np.isnan(hist).any():
        idx = np.arange(len(hist))
        bueno = ~np.isnan(hist)
        hist[~bueno] = np.interp(idx[~bueno], idx[bueno], hist[bueno])
    if len(hist) < MIN_CONTEXTO:
        return None
    return hist.astype(np.float32)


def cargar_modelo():
    """predecir(series, h) -> lista de arrays, y el nombre del checkpoint."""
    from timesfm3 import ModelConfig, TimesFM3Evaluator

    modelo = TimesFM3Evaluator(ModelConfig(checkpoint_path=CHECKPOINT,
                                           per_core_batch_size=32, device='cpu'))

    def predecir(series, h):
        # predict_batch devuelve ForecastOutput (forecast / quantiles / ts_id), no
        # arrays: hay que sacar .forecast o la aritmetica posterior explota.
        return [np.asarray(o.forecast, dtype=float).reshape(-1)[:h] for o in
                modelo.predict_batch(series, horizon=h, return_quantiles=False,
                                     use_symmetric_averaging=False)]

    return predecir, CHECKPOINT


def _rutas_activas(panel, t_max):
    """Las mismas que proyectaria LightGBM: con vuelos en los ultimos 12 meses.

    Se replica el criterio en vez de importarlo porque tiene que ser identico. Si
    los dos modelos proyectaran conjuntos distintos de rutas, cambiar de motor
    moveria el mapa por dos motivos a la vez y no se sabria cual es cual. Y sin
    este filtro se reviven rutas muertas -- las de El Palomar -- y el mapa dibuja
    trafico que no va a existir.
    """
    reciente = panel[(panel['t'] > t_max - MESES_ACTIVIDAD)
                     & (panel['vuelos'].fillna(0) > 0)]
    return set(map(tuple, reciente[['tipo', 'ruta']].drop_duplicates().values))


def rutas_proyectadas(panel=None, meses=HORIZONTE, verbose=True):
    """Filas con la misma forma que devuelve proyeccion_forecast.rutas_proyectadas."""
    panel = cargar_panel() if panel is None else panel
    con_dato = panel[panel['pax'].notna() | panel['vuelos'].notna()]
    t_max = int(con_dato['t'].max())
    activas = _rutas_activas(panel, t_max)

    predecir, _ = cargar_modelo()

    pred = {}
    for metrica in ('vuelos', 'pax'):
        mat = _matriz_serie(panel, metrica)
        mat = mat.loc[:, mat.columns <= t_max]

        contextos, claves = [], []
        for clave in mat.index:
            if tuple(clave) not in activas:
                continue
            ctx = _contexto(mat.loc[clave].values)
            if ctx is None:
                continue
            contextos.append(ctx)
            claves.append(tuple(clave))
        if not contextos:
            continue

        t0 = time.time()
        salidas = predecir(contextos, meses)
        if verbose:
            print('%-7s %d rutas, %.0f s' % (metrica, len(contextos), time.time() - t0),
                  flush=True)
        for clave, p in zip(claves, salidas):
            # Ni vuelos ni pasajeros pueden ser negativos. TimesFM no lo sabe: es un
            # modelo de series genericas, sin nocion de que representa el numero.
            pred.setdefault(clave, {})[metrica] = np.clip(np.asarray(p, float), 0, None)

    filas = []
    for (tipo, ruta), vals in sorted(pred.items()):
        vuelos = vals.get('vuelos')
        if vuelos is None:
            # Sin vuelos no hay fila: el mapa saca el combustible multiplicando el
            # consumo por vuelo, asi que una fila con pax y sin vuelos no aporta nada.
            continue
        pax = vals.get('pax')
        origin, dest = ruta.split(' - ', 1)
        for h in range(meses):
            v = int(round(float(vuelos[h])))
            # Cero vuelos proyectados es ruido, no informacion: no se dibuja.
            if v <= 0:
                continue
            t = t_max + 1 + h
            p = None
            if pax is not None and not np.isnan(pax[h]):
                p = int(round(float(pax[h])))
            filas.append({'tipo': tipo, 'origin': origin, 'dest': dest,
                          'year': str(ANIO_BASE + t // 12),
                          'month': MESES_TXT[t % 12],
                          'vuelos': v, 'pax': p, 'proyectado': True})
    return filas


def descripcion():
    """Que motor se uso, para que quede escrito en el JSON."""
    return CHECKPOINT


if __name__ == '__main__':
    if hasattr(sys.stdout, 'reconfigure'):
        sys.stdout.reconfigure(encoding='utf-8', errors='replace')
    filas = rutas_proyectadas()
    print('filas: %d | rutas: %d'
          % (len(filas), len({(f['origin'], f['dest']) for f in filas})))
