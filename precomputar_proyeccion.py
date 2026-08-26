# -*- coding: utf-8 -*-
"""Calcula la proyección y la deja guardada en `proyeccion_precomputada.json`.

Esto corre EN TU MÁQUINA, no en Render. El entrenamiento pica en ~280 MB de RAM
y la instancia tiene 512 MB contando Flask, el histórico y el JSON de /api/data;
el detalle de por qué está en el docstring de `proyeccion_archivo.py`.

Uso típico, después de subir una planilla nueva de ANAC:

    python precomputar_proyeccion.py
    git add proyeccion_precomputada.json && git commit -m "Proyección al <mes>"
    git push

El servidor no necesita pandas ni lightgbm para servirla: sólo lee el JSON.

Elegir la base
--------------
Por defecto usa la misma búsqueda que la app (RENDER_DISK_PATH, DATABASE_URL,
instance/). Si los datos frescos los tenés en la instancia local, apuntá ahí:

    python precomputar_proyeccion.py --db ../MAPA-NEGOCIO-LOCAL/instance/local_dev.db

El JSON guarda hasta qué mes real se entrenó, así que si la base de producción
tiene meses que este archivo no vio, /modelo lo dice en vez de mostrar un futuro
viejo como si fuera de hoy.
"""

import argparse
import json
import os
import sys
import time

BASE = os.path.dirname(os.path.abspath(__file__))
SALIDA = os.path.join(BASE, 'proyeccion_precomputada.json')

MESES_TXT = ['Ene', 'Feb', 'Mar', 'Abr', 'May', 'Jun',
             'Jul', 'Ago', 'Sep', 'Oct', 'Nov', 'Dic']


def _versiones():
    import lightgbm
    import numpy
    import pandas
    return {'lightgbm': lightgbm.__version__,
            'pandas': pandas.__version__,
            'numpy': numpy.__version__,
            'python': '%d.%d.%d' % sys.version_info[:3]}


def _escribir(path, cabecera, filas):
    """JSON con la cabecera indentada y una fila por línea.

    Una fila por línea es a propósito: el archivo se versiona en git y se
    regenera cada mes, y un diff de 2.000 filas en una sola línea no se puede
    revisar. Así se ve qué rutas cambiaron.
    """
    tmp = path + '.tmp'
    with open(tmp, 'w', encoding='utf-8') as fh:
        fh.write('{\n')
        for clave, valor in cabecera.items():
            fh.write('  %s: %s,\n' % (json.dumps(clave, ensure_ascii=False),
                                      json.dumps(valor, ensure_ascii=False)))
        fh.write('  "filas": [\n')
        for i, f in enumerate(filas):
            coma = ',' if i < len(filas) - 1 else ''
            fh.write('    %s%s\n' % (json.dumps(f, ensure_ascii=False, sort_keys=True), coma))
        fh.write('  ]\n}\n')
    os.replace(tmp, path)   # atómico: nunca deja un archivo a medio escribir


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--db', help='Archivo SQLite (o URL) con la tabla route_monthly. '
                                 'Por defecto, la misma que buscaría la app.')
    ap.add_argument('--meses', type=int, default=12, help='Horizonte, hasta 12 (default: 12).')
    ap.add_argument('--salida', default=SALIDA, help='Dónde escribir el JSON.')
    args = ap.parse_args()

    if hasattr(sys.stdout, 'reconfigure'):
        sys.stdout.reconfigure(encoding='utf-8', errors='replace')

    try:
        import proyeccion_forecast as pf
    except ImportError as e:
        print('No se pudo importar el modelo: %s' % e)
        print('Este script necesita numpy, pandas y lightgbm:')
        print('    pip install -r requirements-modelo.txt')
        return 1

    if args.db:
        url = args.db if '://' in args.db else 'sqlite:///' + os.path.abspath(args.db)
        pf.usar_base(url)
        print('base    : %s' % url)

    t0 = time.time()
    panel = pf.cargar_panel()
    filas = pf.rutas_proyectadas(panel, meses=args.meses)
    tardo = time.time() - t0

    if not filas:
        print('El modelo no devolvió ninguna fila. No se escribe nada: dejar el '
              'archivo anterior es mejor que reemplazarlo por uno vacío.')
        return 1

    con_dato = panel[panel['pax'].notna() | panel['vuelos'].notna()]
    base_t = int(con_dato['t'].max())
    cabecera = {
        'generado': time.strftime('%Y-%m-%dT%H:%M:%S'),
        'versiones': _versiones(),
        'meses': args.meses,
        'base': {'t': base_t,
                 'anio': 2001 + base_t // 12,
                 'mes': (base_t % 12) + 1,
                 'periodo': '%s %d' % (MESES_TXT[base_t % 12], 2001 + base_t // 12)},
        'panel': {'filas': int(len(panel)),
                  'rutas': int(panel.groupby(['tipo', 'ruta']).ngroups)},
    }
    _escribir(args.salida, cabecera, filas)

    periodos = pf.periodos_proyectados(filas)
    print('último real : %s' % cabecera['base']['periodo'])
    print('proyectado  : %d filas, %d rutas, %d períodos (%.1f s)'
          % (len(filas), len({(f['origin'], f['dest']) for f in filas}),
             len(periodos), tardo))
    print('escrito     : %s (%.0f KB)'
          % (args.salida, os.path.getsize(args.salida) / 1024))
    print()
    print('Acordate de commitear el archivo: el deploy lo sirve tal cual, sin recalcular.')
    return 0


if __name__ == '__main__':
    sys.exit(main())
