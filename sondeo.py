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


REINTENTOS_REGISTRO_S = (300, 1800, 7200)   # 5 min, 30 min, 2 h -- despues deja de intentar
LOTE_REGISTRO = 5000                        # filas por commit, para no abrir una transaccion gigante


def _construir_registro_intento():
    """Un intento de bajar y parsear el registro. Devuelve True si quedo listo.

    El CSV completo son ~100 MB y 609.000 filas con 27 columnas; de eso hacen falta dos, y
    solo interesan las filas con `typecode`: sin eso no sirven para estimar el avion, y son
    ~100.000 de las 609.000.

    MEMORIA CONSTANTE A PROPOSITO. La version anterior hacia `r.read()` (94 MB de bytes),
    `.decode()` (otra copia como str) y `io.StringIO(...)` (otra copia mas) y las mantenia
    las tres vivas mientras iteraba: ~280 MB de pico encima de un proceso Flask/gunicorn ya
    cargado, en una instancia chica eso es OOM. Aca se streamea la respuesta a un archivo
    temporal en disco y se parsea leyendolo linea a linea con `csv.reader`: el pico es de
    megabytes, no de cientos.

    Se escribe en un archivo `.parcial` y se renombra (`os.replace`) recien al final: si el
    proceso muere a mitad de camino no queda una base a medio llenar que el lookup daria
    por buena. El commit es por lotes (`LOTE_REGISTRO` filas), no una transaccion unica,
    para no acumular todo el trabajo sin confirmar en memoria/journal.

    EL DESTINO ES SIEMPRE EL DISCO, no `msrtic.base_aviones()`: esa funcion ahora cae a
    la semilla de datos/ cuando el disco todavia no tiene nada, y esa semilla YA EXISTE
    en el repo. Si destino fuera el resultado de la cascada, el `os.path.exists(destino)`
    de aca abajo daria True contra la semilla y esta descarga -- que es la que trae el
    volcado COMPLETO, con matriculas extranjeras -- no correria nunca.
    """
    destino = os.path.join(msrtic.DISCO, 'aircraft_db.sqlite')
    if os.path.exists(destino):
        ESTADO['registro'] = 'listo'
        return True

    # EL DISCO PUEDE NO ESTAR DONDE EL CODIGO CREE. render.yaml declara el disco en
    # /var/data, pero el servicio real se creo a mano y no se administra con ese
    # blueprint -- asi que existe la posibilidad real de que no este montado ahi, o que
    # RENDER_DISK_PATH no este seteada. Sin este chequeo, un fallo de disco se ve
    # identico a un OOM en el mensaje de error, y no hay forma de distinguirlos sin
    # adivinar.
    if not os.path.isdir(msrtic.DISCO):
        ESTADO['registro'] = ('fallo: el directorio del disco no existe: %r '
                               '(revisar montaje / RENDER_DISK_PATH)' % msrtic.DISCO)
        return False
    if not os.access(msrtic.DISCO, os.W_OK):
        ESTADO['registro'] = 'fallo: sin permiso de escritura en %r' % msrtic.DISCO
        return False

    tmp = destino + '.parcial'
    con = None
    try:
        ESTADO['registro'] = 'descargando'
        con = sqlite3.connect(tmp)
        con.execute('create table aircraft (registration text, typecode text)')
        n = 0
        lote = []
        with urllib.request.urlopen(CSV_OPENSKY, timeout=300) as r:
            texto = io.TextIOWrapper(r, encoding='utf-8', errors='replace', newline='')
            for f in csv.DictReader(texto):
                typecode = (f.get('typecode') or '').strip()
                if not typecode:
                    continue
                registration = (f.get('registration') or '').strip()
                lote.append((registration, typecode))
                if len(lote) >= LOTE_REGISTRO:
                    con.executemany('insert into aircraft values (?, ?)', lote)
                    con.commit()
                    n += len(lote)
                    lote = []
            if lote:
                con.executemany('insert into aircraft values (?, ?)', lote)
                con.commit()
                n += len(lote)
        con.execute('create index idx_reg on aircraft (registration)')
        con.commit()
        con.close()
        con = None
        os.replace(tmp, destino)
        ESTADO['registro'] = 'listo (%d aeronaves)' % n
        return True
    except Exception as exc:                           # noqa: BLE001
        # Sin registro, MS RTIC estima TODOS los aviones con el modelo del mapa: se
        # pierde precision, no funcionalidad.
        ESTADO['registro'] = 'fallo: %s: %s' % (type(exc).__name__, exc)
        return False
    finally:
        if con is not None:
            try:
                con.close()
            except sqlite3.Error:
                pass
        try:
            if os.path.exists(tmp):
                os.remove(tmp)
        except OSError:
            pass


def _construir_registro():
    """Reintenta con backoff hasta que quede listo; deja de intentar cuando lo logra.

    Un solo disparo por proceso significaba que, si fallaba, ESTADO quedaba en 'fallo'
    hasta el proximo redeploy -- nadie lo iba a notar sin mirar el JSON. Los tiempos de
    `REINTENTOS_REGISTRO_S` (5 min, 30 min, 2 h) no son un loop cerrado: son pocos
    intentos espaciados, pensados para problemas transitorios de red, no para machacar
    un fallo permanente (disco mal montado, por ejemplo) cada pocos segundos.
    """
    if _construir_registro_intento():
        return
    for espera in REINTENTOS_REGISTRO_S:
        time.sleep(espera)
        if _construir_registro_intento():
            return


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
