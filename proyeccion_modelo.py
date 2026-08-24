# -*- coding: utf-8 -*-
"""Modelo global de proyeccion por ruta (LightGBM) + backtest contra baselines.

DECISIONES DE DISENO, y por que
-------------------------------

1. UN SOLO MODELO PARA TODAS LAS RUTAS, no uno por ruta. La ruta entra como
   variable categorica. Con ~180 meses utiles por ruta y 12 dummies de mes, un
   modelo por ruta se sobreajusta seguro; el modelo global comparte la forma
   estacional y la reaccion al ciclo entre las 260 rutas.

2. EL TARGET ES UN RATIO INTERANUAL EN LOGS, NO EL NIVEL. Los arboles no
   extrapolan: fuera del rango visto en entrenamiento predicen constante, asi
   que un modelo entrenado sobre pasajeros absolutos topea en el maximo
   historico de cada ruta. Predecimos  log((y_t + 1) / (y_{t-12} + 1))  y
   despues reconstruimos el nivel. Efecto lateral util: el naive estacional es
   exactamente "predecir ratio = 0", asi que el modelo compite contra la
   baseline en la misma escala y se ve de una si aporta algo.

3. HORIZONTE DIRECTO, NO RECURSIVO. Se entrena con el horizonte h (1..12) como
   feature y solo con informacion disponible en el origen T. Nada de alimentar
   predicciones propias como si fueran datos: el error no se realimenta.

4. LOS QUIEBRES ESTRUCTURALES NO SE PROMEDIAN. Las ventanas de test que caen
   sobre 2020-2021 se excluyen del backtest (medir contra la pandemia es medir
   prediccion de pandemia, no de demanda) y el regimen entra como feature para
   que el modelo sepa en cual esta parado.

Todo lo que se mide es MASE: error absoluto medio dividido por el error del
naive estacional dentro de la muestra. MASE < 1 es mejor que el naive; se
reporta la MEDIANA entre rutas, no el promedio, porque cuatro rutas chicas con
error enorme tapan el resultado de las 250 restantes.
"""

import sys
import warnings

import numpy as np
import pandas as pd

from proyeccion_datos import ANIO_BASE, cargar_panel, ultimo_periodo

warnings.filterwarnings('ignore', category=FutureWarning)

HORIZONTES = list(range(1, 13))     # proyectamos 12 meses hacia adelante
COVID = (228, 251)                  # t de 2020-01 a 2021-12, ambos inclusive

PARAMS_LGBM = dict(
    objective='regression_l1',      # L1: robusta a los outliers de rutas chicas
    n_estimators=400,
    learning_rate=0.05,
    num_leaves=31,
    min_child_samples=40,
    subsample=0.8,
    subsample_freq=1,
    colsample_bytree=0.8,
    reg_lambda=1.0,
    verbose=-1,
)


# --------------------------------------------------------------------------
# Features
# --------------------------------------------------------------------------

def _matriz_serie(panel, metrica):
    """Pivotea el panel a una matriz (ruta x t) para poder indexar por lags."""
    m = panel.pivot_table(index=['tipo', 'ruta'], columns='t', values=metrica,
                          aggfunc='first')
    t_min, t_max = int(m.columns.min()), int(m.columns.max())
    return m.reindex(columns=pd.RangeIndex(t_min, t_max + 1))


def _suma(mat, t_ini, t_fin):
    """Suma inclusive [t_ini, t_fin] por fila; NaN si la ventana cae afuera."""
    cols = [c for c in range(t_ini, t_fin + 1) if c in mat.columns]
    if not cols:
        return pd.Series(np.nan, index=mat.index)
    return mat[cols].sum(axis=1, min_count=1)


def _col(mat, t):
    if t in mat.columns:
        return mat[t]
    return pd.Series(np.nan, index=mat.index)


def _toca_covid(t_ini, t_fin):
    """True si la ventana [t_ini, t_fin] se solapa con 2020-2021."""
    return not (t_fin < COVID[0] or t_ini > COVID[1])


def _ratio(num, den, ventana_num, ventana_den):
    """Ratio interanual en logs, NaN si alguna de las dos ventanas es pandemia.

    Sin esto, el origen dic-2022 le entrega al modelo un crecimiento interanual
    mediano de +207% -- que no es demanda creciendo, es el rebote contra el
    piso de 2021. El modelo aprendio en 20 anos que impulso alto anticipa mas
    crecimiento, y proyecta el rebote hacia adelante: en el backtest eso
    multiplicaba por 3 el error de 2023. LightGBM maneja NaN de forma nativa
    (lo rutea a su propia rama), asi que devolver NaN es literalmente decirle
    "para esta ruta no hay referencia confiable", que es la verdad.
    """
    r = np.log((num + 1) / (den + 1))
    if _toca_covid(*ventana_num) or _toca_covid(*ventana_den):
        return pd.Series(np.nan, index=r.index)
    return r


def construir_muestras(mat, origen, horizontes=HORIZONTES, con_target=True):
    """Arma las filas (ruta, h) para un origen T dado.

    Solo usa informacion con t <= T. El ancla es y_{T+h-12}, que para h <= 12
    siempre es un mes ya observado: por eso el modelo puede aprender el desvio
    respecto del naive estacional sin mirar el futuro.
    """
    T = origen

    y_T = _col(mat, T)
    y_T12 = _col(mat, T - 12)
    s12 = _suma(mat, T - 11, T)
    s24 = _suma(mat, T - 23, T - 12)
    s3 = _suma(mat, T - 2, T)
    s3_ant = _suma(mat, T - 14, T - 12)

    r_ult = _ratio(y_T, y_T12, (T, T), (T - 12, T - 12))
    r_3m = _ratio(s3, s3_ant, (T - 2, T), (T - 14, T - 12))
    r_12m = _ratio(s12, s24, (T - 11, T), (T - 23, T - 12))
    nivel = np.log1p(s12 / 12.0)

    ventana24 = [c for c in range(T - 23, T + 1) if c in mat.columns]
    activos24 = (mat[ventana24].gt(0).sum(axis=1) if ventana24
                 else pd.Series(0, index=mat.index))

    # volatilidad: desvio de los ratios interanuales de los ultimos 12 meses,
    # salteando los que comparan contra meses de pandemia.
    ratios = [_ratio(_col(mat, T - k), _col(mat, T - k - 12),
                     (T - k, T - k), (T - k - 12, T - k - 12))
              for k in range(12)]
    vol = pd.concat(ratios, axis=1).std(axis=1)

    # Cuanta de la informacion del origen quedo inutilizada por la pandemia:
    # le permite al modelo distinguir "no tengo referencia" de "creci cero".
    sin_ref = float(_toca_covid(T - 23, T))

    hay = mat.notna()
    primero = hay.idxmax(axis=1).where(hay.any(axis=1))
    antiguedad = T - primero

    filas = []
    for h in horizontes:
        t_obj = T + h
        ancla = _col(mat, t_obj - 12)                 # mismo mes, ano anterior
        media_ancla = _suma(mat, t_obj - 23, t_obj - 12) / 12.0
        forma_seas = _ratio(ancla, media_ancla,
                            (t_obj - 12, t_obj - 12), (t_obj - 23, t_obj - 12))

        d = pd.DataFrame({
            'h': h,
            'mes_obj': (t_obj % 12) + 1,
            'anio_obj': ANIO_BASE + t_obj // 12,
            'r_ult': r_ult, 'r_3m': r_3m, 'r_12m': r_12m,
            'nivel': nivel, 'vol': vol,
            'activos24': activos24, 'antiguedad': antiguedad,
            'forma_seas': forma_seas, 'ancla': ancla,
            'post_covid': int(t_obj > COVID[1]),
            'sin_ref': sin_ref,
            # el ancla misma cae en pandemia: el naive estacional arranca de un
            # piso irreal y el modelo tiene que poder saberlo.
            'ancla_covid': float(_toca_covid(t_obj - 12, t_obj - 12)),
            't_obj': t_obj, 'origen': T,
        }, index=mat.index)

        if con_target:
            d['y'] = _col(mat, t_obj)
            d['target'] = np.log((d['y'] + 1) / (ancla + 1))

        filas.append(d.reset_index())

    out = pd.concat(filas, ignore_index=True)
    # Sin ancla no hay nada que corregir. r_12m ya puede ser NaN a proposito
    # (ventana pandemica), asi que el filtro va sobre el nivel, que solo falta
    # cuando la ruta no tiene 12 meses de historia.
    out = out[out['ancla'].notna() & out['nivel'].notna()]
    if con_target:
        out = out[out['target'].notna()]
    return out


FEATURES = ['h', 'mes_obj', 'r_ult', 'r_3m', 'r_12m', 'nivel', 'vol',
            'activos24', 'antiguedad', 'forma_seas', 'post_covid',
            'sin_ref', 'ancla_covid', 'tipo', 'ruta']
CATEGORICAS = ['tipo', 'ruta']


def _preparar(df, categorias=None):
    X = df[FEATURES].copy()
    for c in CATEGORICAS:
        X[c] = X[c].astype('category')
        if categorias is not None:
            X[c] = X[c].cat.set_categories(categorias[c])
    return X


# --------------------------------------------------------------------------
# Backtest
# --------------------------------------------------------------------------

def _origenes_validos(mat, min_anio=2006):
    """Diciembres cuya ventana de test no toca la pandemia y tiene datos."""
    t_max = int(mat.columns.max())
    origenes = []
    for anio in range(min_anio, 2030):
        T = (anio - ANIO_BASE) * 12 + 11        # diciembre
        if T + 1 > t_max:
            break
        if not (T + 12 < COVID[0] or T + 1 > COVID[1]):
            continue                            # la ventana pisa 2020-2021
        origenes.append(T)
    return origenes


def _escalas_mase(mat):
    """MAE del naive estacional dentro de la muestra, por ruta."""
    escalas = {}
    for idx, fila in mat.iterrows():
        v = fila.values.astype(float)
        d = np.abs(v[12:] - v[:-12])
        d = d[~np.isnan(d)]
        escalas[idx] = np.mean(d) if len(d) else np.nan
    return escalas


def _mase_por_ruta(escalas, pred_df, columna_pred):
    out = []
    for (tipo, ruta), g in pred_df.groupby(['tipo', 'ruta']):
        esc = escalas.get((tipo, ruta), np.nan)
        if not esc or np.isnan(esc):
            continue
        out.append(np.abs(g['y'] - g[columna_pred]).mean() / esc)
    return np.array(out)


def backtest(metrica='pax', panel=None, verbose=True):
    import lightgbm as lgb

    panel = cargar_panel() if panel is None else panel
    mat = _matriz_serie(panel, metrica)
    origenes = _origenes_validos(mat)

    if verbose:
        print('  ventanas de test: %s' % ', '.join(
            str(ANIO_BASE + (o + 1) // 12) for o in origenes))

    resultados = []
    for T in origenes:
        # Entrenar solo con pares cuyo target ya ocurrio antes del origen.
        entren = []
        for T2 in range(T - 12 * 20, T - 11, 3):    # origenes cada 3 meses
            if T2 - 24 < int(mat.columns.min()):
                continue
            m = construir_muestras(mat, T2)
            m = m[m['t_obj'] <= T]
            if len(m):
                entren.append(m)
        if not entren:
            continue
        entren = pd.concat(entren, ignore_index=True)
        # El modelo no debe aprender la caida de la pandemia como estacionalidad.
        entren = entren[~entren['t_obj'].between(*COVID)]

        prueba = construir_muestras(mat, T)
        prueba = prueba[prueba['t_obj'] > T]
        if not len(prueba) or not len(entren):
            continue

        cats = {c: pd.CategoricalDtype(
                    sorted(set(entren[c]) | set(prueba[c]))).categories
                for c in CATEGORICAS}

        modelo = lgb.LGBMRegressor(**PARAMS_LGBM)
        modelo.fit(_preparar(entren, cats), entren['target'],
                   categorical_feature=CATEGORICAS)

        # Regla de seguridad: si la ruta no tiene NINGUNA referencia interanual
        # valida (paso el ano de pandemia), el modelo esta corrigiendo a ciegas.
        # En 2023 -- el primer ano cuyo mes espejo cae entero en 2021 -- eso
        # multiplicaba por 6 el error. Sin referencia no se corrige: se deja el
        # naive estacional, que es la respuesta honesta a "no se".
        a_ciegas = prueba['r_12m'].isna() & prueba['r_ult'].isna()
        crudo = (prueba['ancla'] + 1) * np.exp(
            modelo.predict(_preparar(prueba, cats))) - 1

        prueba = prueba.assign(
            pred_lgbm=np.where(a_ciegas, prueba['ancla'], crudo),
            pred_snaive=prueba['ancla'],
            # si r_12m quedo NaN por pandemia, esta baseline degrada a snaive
            pred_snaive_g=(prueba['ancla'] + 1) * np.exp(
                prueba['r_12m'].fillna(0.0).clip(-0.7, 0.7)) - 1,
        )

        escalas = _escalas_mase(mat.loc[:, mat.columns <= T])
        for col, nombre in [('pred_snaive', 'snaive'),
                            ('pred_snaive_g', 'snaive*crec'),
                            ('pred_lgbm', 'lightgbm')]:
            v = _mase_por_ruta(escalas, prueba, col)
            resultados.append({'anio_test': ANIO_BASE + (T + 1) // 12,
                               'modelo': nombre,
                               'mase_mediana': np.median(v), 'rutas': len(v)})

    return pd.DataFrame(resultados)


def _tabla(res, metrica):
    piv = res.pivot_table(index='anio_test', columns='modelo', values='mase_mediana')
    piv = piv[[c for c in ['snaive', 'snaive*crec', 'lightgbm'] if c in piv.columns]]
    print()
    print('== %s == MASE mediana entre rutas (menor es mejor, <1 le gana al naive)'
          % metrica.upper())
    print(piv.round(2).to_string())
    print('-' * 52)
    print('promedio   ' + '   '.join('%s=%.2f' % (c, piv[c].mean()) for c in piv.columns))
    delta = 100 * (piv['lightgbm'].mean() / piv['snaive'].mean() - 1)
    print('lightgbm vs snaive: %+.1f%% de error' % delta)
    return piv


if __name__ == '__main__':
    if hasattr(sys.stdout, 'reconfigure'):
        sys.stdout.reconfigure(encoding='utf-8', errors='replace')

    panel = cargar_panel()
    anio, mes, _ = ultimo_periodo(panel)
    print('panel: %d filas, %d rutas, hasta %d-%02d'
          % (len(panel), panel.groupby(['tipo', 'ruta']).ngroups, anio, mes))

    for metrica in ('pax', 'vuelos'):
        print()
        print('entrenando %s ...' % metrica)
        _tabla(backtest(metrica, panel=panel), metrica)
