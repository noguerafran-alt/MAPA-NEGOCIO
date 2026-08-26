# -*- coding: utf-8 -*-
"""Lee la proyección ya calculada desde `proyeccion_precomputada.json`.

POR QUÉ UN ARCHIVO Y NO ENTRENAR EN EL SERVIDOR
-----------------------------------------------
Medido en esta misma máquina, con un solo hilo (que es lo que se parece a los
0.5 CPU de Render), entrenar los dos modelos cuesta:

    importar numpy + pandas .............  50 MB de RSS
    importar lightgbm ...................  70 MB más   (141 MB acumulados)
    armar el panel (69.000 filas) .......  pico de 198 MB
    entrenar + predecir .................  pico de 280 MB, queda en 183 MB
    tiempo ..............................  12 s a un hilo (~25-30 s a 0.5 CPU)

La instancia tiene 512 MB en total, y ahí adentro también entran Flask, el
histórico 2001-2022, el ORM y el JSON que arma /api/data. Entrenar dentro del
proceso web deja el servicio a un upload de /admin de distancia del OOM killer,
y además paga los 25-30 s en la primera visita después de CADA reinicio, porque
el caché del modelo vive en memoria y se pierde con el proceso.

Este módulo no importa nada fuera de la stdlib. Leer la proyección cuesta lo
que pesa el JSON (~250 KB) y nada más: cero pandas, cero entrenamiento.

El archivo lo genera `precomputar_proyeccion.py` desde una máquina con RAM.

QUÉ PASA CUANDO EL ARCHIVO QUEDA VIEJO
--------------------------------------
La base sigue creciendo con cada planilla que se sube por /admin, así que el
archivo se desactualiza solo. Dos reglas para que eso no mienta:

  1. Un mes proyectado que YA tiene dato real no se devuelve. Si no, el mapa
     sumaría la proyección encima del dato de ANAC y el mes contaría doble.
  2. `estado()` reporta hasta dónde llegaban los datos cuando se generó, para
     que /modelo pueda decir "esto se calculó con datos hasta junio y ya hay
     julio cargado" en vez de mostrar un futuro viejo como si fuera de hoy.
"""

import json
import os
import threading

BASE = os.path.dirname(os.path.abspath(__file__))
ARCHIVO = os.path.join(BASE, 'proyeccion_precomputada.json')

MESES_TXT = ['Ene', 'Feb', 'Mar', 'Abr', 'May', 'Jun',
             'Jul', 'Ago', 'Sep', 'Oct', 'Nov', 'Dic']

ANIO_BASE = 2001  # mismo origen del índice t que usa proyeccion_datos.py

_cache = {'mtime': None, 'path': None, 'datos': None}
_lock = threading.Lock()


def t_de(anio, mes):
    """Índice temporal continuo en meses desde enero de ANIO_BASE.

    `mes` puede venir como número (1-12) o como el texto de ANAC ('Ene').
    Devuelve None si el mes no se entiende, para que una fila corrupta se
    saltee en vez de tirar abajo el cálculo del último período real.
    """
    if isinstance(mes, str):
        if mes not in MESES_TXT:
            return None
        m = MESES_TXT.index(mes) + 1
    else:
        m = int(mes)
    try:
        return (int(anio) - ANIO_BASE) * 12 + (m - 1)
    except (TypeError, ValueError):
        return None


def cargar(path=None):
    """Contenido del archivo, o None si no está. Relee si cambió en el disco."""
    path = path or ARCHIVO
    try:
        mtime = os.path.getmtime(path)
    except OSError:
        with _lock:
            _cache.update(mtime=None, path=path, datos=None)
        return None

    with _lock:
        if _cache['datos'] is not None and _cache['path'] == path and _cache['mtime'] == mtime:
            return _cache['datos']
        try:
            with open(path, encoding='utf-8') as fh:
                datos = json.load(fh)
        except (OSError, ValueError):
            # Un archivo roto no puede tirar abajo el mapa: es una capa extra.
            _cache.update(mtime=None, path=path, datos=None)
            return None
        _cache.update(mtime=mtime, path=path, datos=datos)
        return datos


def disponible(path=None):
    datos = cargar(path)
    return bool(datos and datos.get('filas'))


def rutas_proyectadas(ultimo_real_t=None, path=None):
    """Filas listas para sumar a `all_routes` en /api/data.

    Salen con la misma forma que las de RouteMonthly más el flag `proyectado`,
    que es lo que ya esperaba el resto de la app.

    `ultimo_real_t` es el índice t del último mes con dato real EN ESTA BASE.
    Todo mes proyectado que no sea posterior a ese se descarta: si la planilla
    de julio ya se subió, el julio proyectado sobra y sumarlo contaría doble.
    """
    datos = cargar(path)
    if not datos:
        return []
    filas = datos.get('filas') or []
    if ultimo_real_t is None:
        return list(filas)
    return [f for f in filas
            if (t_de(f.get('year'), f.get('month')) or -1) > ultimo_real_t]


def periodos_proyectados(filas):
    """Claves 'AAAA-Mmm' que son proyección, para que el frontend las marque."""
    return sorted({'%s-%s' % (f['year'], f['month']) for f in filas})


def _bonito(t):
    return '%s %d' % (MESES_TXT[t % 12], ANIO_BASE + t // 12)


def estado(ultimo_real_t=None, path=None):
    """Resumen del archivo para /modelo y /api/deploy_status.

    `desactualizada` no significa "rota": significa que desde que se generó se
    cargaron meses nuevos, así que el modelo no los vio y conviene regenerarla.
    """
    datos = cargar(path)
    if not datos:
        return {'hay_archivo': False}

    base_t = (datos.get('base') or {}).get('t')
    filas = rutas_proyectadas(ultimo_real_t, path)
    periodos = sorted(t for t in {t_de(f['year'], f['month']) for f in filas}
                      if t is not None)

    return {
        'hay_archivo': True,
        'generado': datos.get('generado'),
        'versiones': datos.get('versiones'),
        'base_t': base_t,
        'base_periodo': _bonito(base_t) if base_t is not None else None,
        'n_filas': len(filas),
        'n_filas_archivo': len(datos.get('filas') or []),
        'n_rutas': len({(f['origin'], f['dest']) for f in filas}),
        'n_rutas_panel': (datos.get('panel') or {}).get('rutas'),
        'n_filas_panel': (datos.get('panel') or {}).get('filas'),
        'desde': _bonito(periodos[0]) if periodos else None,
        'hasta': _bonito(periodos[-1]) if periodos else None,
        'desactualizada': (base_t is not None and ultimo_real_t is not None
                           and ultimo_real_t > base_t),
        'ultimo_real': _bonito(ultimo_real_t) if ultimo_real_t is not None else None,
    }
