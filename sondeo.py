# -*- coding: utf-8 -*-
r"""El poller de AA2000, adentro del proceso web. Mantiene MS RTIC con datos frescos.

POR QUE ACA SI, Y EN EL MAPA LOCAL NO
-------------------------------------
El mapa local no puede sondear: su regla 2 es que arranca sin internet, asi que alla el
radar corre afuera y deja la base. Aca es al reves -- esto ES un servicio online -- y el
sondeo vive adentro, sin depender de que alguien deje un .bat abierto en una PC.

POR QUE UN HILO Y NO UN CRON DE RENDER
--------------------------------------
Mirando la configuracion real del servicio (render.yaml):

  plan: starter          no se suspende por inactividad, asi que el hilo no duerme
  --workers 1            UNA sola instancia: el hilo no se duplica. Con 2+ workers
                         habria dos pollers escribiendo la misma base
  disk /var/data 1GB     el acumulado sobrevive a los deploys (el resto del
                         filesystem de Render es efimero)

Con eso, un hilo es cero infraestructura nueva, y aa2000.py es **stdlib pura**: no agrega
ni una dependencia a requirements.txt.

**Si algun dia se sube a 2 workers, esto hay que mover a un cron.** No es preferencia de
estilo: dos pollers sobre la misma SQLite se pisan, y el sintoma no seria un error sino
filas trabadas y sondeos perdidos.

QUE SONDEA
----------
Todo el pais, no Aeroparque. `id_arpt` de la API NO filtra -- pedir AEP, EZE o COR
devuelve la misma lista nacional byte por byte -- asi que `aeropuerto=None` guarda los 28
aeropuertos que el feed publica, y sale de una sola llamada.

EL REGISTRO DE MATRICULAS
-------------------------
`aircraft_db.sqlite` son 52 MB y no puede ir en el repo. Se construye una vez desde
OpenSky, en el disco, y EN BACKGROUND: si el arranque esperara esa descarga, un deploy
tardaria minutos y un fallo de red dejaria el servicio sin levantar. Mientras no este,
MS RTIC funciona igual con TODOS los aviones estimados por el modelo del mapa -- pierde
precision, no funcionalidad, y el estado lo dice.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import csv
import io
import sqlite3
import threading
import time
import urllib.request

import aa2000
import msrtic

INTERVALO_S = 300          # 5 min, igual que ms_local.py del radar

CSV_OPENSKY = 'https://s3.opensky-network.org/data-samples/metadata/aircraftDatabase.csv'

# Que esta haciendo el poller. Se expone en /api/msrtic para que la pagina pueda decir
# "el ultimo sondeo fue hace 3 minutos" en vez de mostrar numeros sin fecha: un dato que
# se presenta como "en vivo" y esta congelado es peor que uno viejo con fecha.
ESTADO = {'ultimo': None, 'proximo': None, 'error': None, 'vueltas': 0,
          'recibidas': 0, 'nuevas': 0, 'corriendo': False,
          'registro': 'sin construir'}

_arranque = threading.Lock()
_ya = {'poller': False, 'registro': False}


def _sondear_siempre():
    """Nunca muere por una excepcion.

    Si una vuelta falla -- se cayo la API, cambio el formato -- se anota en ESTADO y se
    sigue. Que el hilo muriera en silencio dejaria la pagina mostrando el ultimo dato
    bueno para siempre, sin decir que dejo de actualizarse.
    """
    conn = aa2000.abrir(msrtic.base_oficial())
    ESTADO['corriendo'] = True
    while True:
        try:
            t = aa2000.sondear(conn, aeropuerto=None, log=lambda *a: None)
            ESTADO['error'] = '; '.join(t['errores'])[:300] if t['errores'] else None
            ESTADO['recibidas'] = t['recibidas']
            ESTADO['nuevas'] = t['nuevas']
        except Exception as exc:                       # noqa: BLE001
            ESTADO['error'] = '%s: %s' % (type(exc).__name__, exc)
        ESTADO['ultimo'] = time.time()
        ESTADO['proximo'] = ESTADO['ultimo'] + INTERVALO_S
        ESTADO['vueltas'] += 1
        time.sleep(INTERVALO_S)


def _construir_registro():
    """Baja el registro de OpenSky y deja solo las dos columnas que se usan.

    El CSV completo son ~100 MB y 600.000 filas con 27 columnas; de eso hacen falta dos.
    Se escribe en un archivo temporal y se renombra al final: si el proceso muere a mitad
    de la descarga, no queda una base a medio llenar que el lookup daria por buena.

    EL DESTINO ES SIEMPRE EL DISCO, no `msrtic.base_aviones()`: esa funcion ahora cae a
    la semilla de datos/ cuando el disco todavia no tiene nada, y esa semilla YA EXISTE
    en el repo. Si destino fuera el resultado de la cascada, el `os.path.exists(destino)`
    de aca abajo daria True contra la semilla y esta descarga -- que es la que trae el
    volcado COMPLETO, con matriculas extranjeras -- no correria nunca.
    """
    destino = os.path.join(msrtic.DISCO, 'aircraft_db.sqlite')
    if os.path.exists(destino):
        ESTADO['registro'] = 'listo'
        return
    tmp = destino + '.parcial'
    try:
        ESTADO['registro'] = 'descargando'
        with urllib.request.urlopen(CSV_OPENSKY, timeout=300) as r:
            datos = r.read()
        con = sqlite3.connect(tmp)
        con.execute('create table aircraft (registration text, typecode text)')
        lector = csv.DictReader(io.StringIO(datos.decode('utf-8', 'replace')))
        con.executemany('insert into aircraft values (?, ?)',
                        (((f.get('registration') or '').strip(),
                          (f.get('typecode') or '').strip())
                         for f in lector))
        con.execute('create index idx_reg on aircraft (registration)')
        con.commit()
        n = con.execute('select count(*) from aircraft').fetchone()[0]
        con.close()
        os.replace(tmp, destino)
        ESTADO['registro'] = 'listo (%d aeronaves)' % n
    except Exception as exc:                           # noqa: BLE001
        # Sin registro, MS RTIC estima TODOS los aviones con el modelo del mapa: se
        # pierde precision, no funcionalidad.
        ESTADO['registro'] = 'fallo: %s: %s' % (type(exc).__name__, exc)
        try:
            if os.path.exists(tmp):
                os.remove(tmp)
        except OSError:
            pass


def arrancar():
    """Levanta los dos hilos, una sola vez por proceso.

    Idempotente a proposito: gunicorn puede importar el modulo mas de una vez, y dos
    pollers sobre la misma SQLite se pisan.
    """
    with _arranque:
        if not _ya['poller']:
            threading.Thread(target=_sondear_siempre, daemon=True,
                             name='aa2000-poller').start()
            _ya['poller'] = True
        if not _ya['registro']:
            threading.Thread(target=_construir_registro, daemon=True,
                             name='opensky-registro').start()
            _ya['registro'] = True


def estado():
    """Lo que necesita la pagina para poder fechar lo que muestra."""
    d = dict(ESTADO)
    d['intervalo_s'] = INTERVALO_S
    if d['ultimo']:
        d['hace_s'] = int(time.time() - d['ultimo'])
    return d
