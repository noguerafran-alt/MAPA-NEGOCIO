# -*- coding: utf-8 -*-
r"""Arma datos/aircraft_db.sqlite: la SEMILLA del registro de matriculas para el deploy.

    python datos\actualizar_registro_aviones.py [ruta al registro origen]

PARA QUE. `msrtic.base_aviones()` necesita un registro matricula -> tipo de avion para
poder poner el tipo de cada partida (sin el, TODAS las partidas caen en "avion no
identificado", en silencio). En Render el volcado completo vive en el disco persistente
(/var/data/aircraft_db.sqlite) y no en el repo -- pero un deploy nuevo, antes de que
alguien suba ese archivo al disco, se queda sin nada. Esta semilla es el piso: viaja EN
el repo para que ese primer deploy ya resuelva algo en vez de nada.

POR QUE SOLO ARGENTINAS (LV/LQ). El registro completo son ~609.000 aeronaves de todo el
mundo; este repo es PUBLICO y solo hace falta cruzar la flota que opera en Argentina.
Recortando a matriculas que empiezan con LV o LQ (mayuscula o minuscula) y que tengan
typecode quedan ~1.700 filas y ~100 KB, contra los ~50 MB / 9 columnas del original.
Ademas de LV/LQ, se descartan operador, serie y demas: solo quedan `registration` y
`typecode`, que es matricula + modelo de avion -- dato de registro aeronautico PUBLICO
(es lo mismo que se ve pintado en el fuselaje), nada de vuelos ni de negocio.

DE DONDE SALE EL ORIGEN. NO BAJA NADA DE INTERNET. Lee un sqlite ya existente con una
tabla `aircraft(registration, typecode, ...)`, con la ruta por argumento o por la
variable de entorno MS_RTIC_AVIONES_ORIGEN (util para no repetir la ruta cada vez). En
este repo ese origen es el volcado del radar de la antena ("RADAR YPF/tools/
aircraft_db.sqlite"), que baja de OpenSky *en ese otro repo*, no aca.

CUANDO CORRERLO. Cuando la semilla quede vieja y se quiera refrescar la version que
viaja en el repo. No es parte del arranque: `base_aviones()` la usa tal cual esta
commiteada.
"""
import os
import sqlite3
import sys

AQUI = os.path.dirname(os.path.abspath(__file__))
DESTINO = os.path.join(AQUI, 'aircraft_db.sqlite')


def _origen():
    if len(sys.argv) > 1:
        return sys.argv[1]
    return os.environ.get('MS_RTIC_AVIONES_ORIGEN')


def main():
    fuente = _origen()
    if not fuente:
        print('Falta el origen: pasalo como argumento o poné MS_RTIC_AVIONES_ORIGEN.')
        print('  python datos\\actualizar_registro_aviones.py <ruta al aircraft_db.sqlite origen>')
        return 1
    if not os.path.exists(fuente):
        print('NO EXISTE: ' + fuente)
        return 1

    parcial = DESTINO + '.parcial'
    for f in (parcial, parcial + '-wal', parcial + '-shm'):
        if os.path.exists(f):
            os.remove(f)

    o = sqlite3.connect('file:%s?mode=ro' % fuente.replace(os.sep, '/'), uri=True)
    d = sqlite3.connect(parcial)
    try:
        d.execute('CREATE TABLE aircraft (registration TEXT, typecode TEXT)')
        # Solo matriculas argentinas (LV/LQ) con typecode: es lo unico que este repo
        # necesita cruzar, y lo que mantiene la semilla chica y publicable.
        filas = [
            (reg, typ.strip().upper())
            for reg, typ in o.execute(
                "SELECT registration, typecode FROM aircraft "
                "WHERE registration IS NOT NULL AND typecode IS NOT NULL "
                "AND TRIM(registration) <> '' AND TRIM(typecode) <> ''")
            if reg and (reg.strip().upper().startswith('LV')
                        or reg.strip().upper().startswith('LQ'))
        ]
        d.executemany('INSERT INTO aircraft VALUES (?, ?)', filas)
        d.execute('CREATE INDEX idx_reg ON aircraft (registration)')
        d.commit()
        n, = d.execute('SELECT COUNT(*) FROM aircraft').fetchone()
        d.execute('VACUUM')
    finally:
        d.close()
        o.close()

    os.replace(parcial, DESTINO)
    print('%s' % DESTINO)
    print('   %d matriculas argentinas con tipo, %.1f KB'
          % (n, os.path.getsize(DESTINO) / 1024))
    return 0


if __name__ == '__main__':
    sys.exit(main())
