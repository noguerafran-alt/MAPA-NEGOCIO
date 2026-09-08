"""La verdad externa de Aeropuertos Argentina, acumulada para que exista historico.

POR QUE HACE FALTA ACUMULAR. La API de AA2000 tiene dos endpoints y ninguno
solo alcanza:

    flights-fth    la programacion de cualquier fecha, 188 partidas de AEP en
                   un dia, pero SOLO hora programada. Sin estado ni hora real.
    all-flights    el feed de las pantallas: hora real, estado, MATRICULA y
                   PASAJEROS. Pero la ventana arranca en "ahora" y va hacia
                   adelante ~26 h. NO se puede pedir "los despegues del 3 de
                   septiembre con su hora real".

Medido el 2026-09-06 con c=500 sobre AEP/D: 500 registros de 07/09 09:15 a
08/09 10:55, de los cuales 81 traian hora real -el pasado reciente que todavia
esta en la pantalla-, 204 matricula y 114 pasajeros.

O sea que la hora real de un despegue existe en la API durante unas horas y
despues desaparece para siempre. Por eso esto: pasar cada tantos minutos y
guardar lo que se vio, para que en un mes se pueda comparar lo que la antena
detecto contra lo que efectivamente ocurrio.

BASE SEPARADA, A PROPOSITO. No va en adsb_log.db aunque seria mas comodo. Dos
razones y las dos importan:

  1. El poller tiene que poder correr con el grabador apagado y al reves. Un
     solo archivo los ata: dos escritores sobre la misma base se serializan.
  2. Esto es la REFERENCIA contra la que se mide el sistema. Mezclarla en el
     mismo archivo que las mediciones propias hace posible confundirlas en una
     consulta distraida, y ese es el error que arruinaria la comparacion
     entera. Separadas, confundirlas exige un ATTACH explicito.

NO ES UNA API PUBLICA DOCUMENTADA. Responde mandando la cabecera Origin del
propio sitio; sin ella devuelve 401 {"error":"Unauthorized","message":"Invalid
Key"}. Funciona, pero se entra por donde entra el frontend: puede cambiar o
cerrarse sin aviso. Si esto va a sostener algo que se le muestra a un tercero,
conviene pedirle a Aeropuertos Argentina un acceso formal.

Uso:
  python aa2000.py --sondear              una pasada y salir
  python aa2000.py --seguir               quedarse sondeando cada 5 min
  python aa2000.py --resumen              que hay acumulado
"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

BASE = "https://WebAA-API-h4d5amdfcze7hthn.a02.azurefd.net/web-prod/v1/api-aa"

# La cabecera que la API exige. No es un secreto ni una credencial: es el
# Origin del sitio, y el gateway lo usa como lista blanca de CORS. Sin ella,
# 401 "Invalid Key".
ORIGIN = "https://www.aeropuertosargentina.com"

# c es el parametro de CANTIDAD y el default del servidor son 10 registros.
# Medido: c=500 devuelve 500 y cubre ~26 h hacia adelante. Ni limit ni pageSize
# hacen nada -- se probaron los tres y solo c cambio el resultado.
CANTIDAD = int(os.environ.get("AA2000_CANTIDAD", 500))

# Cada cuanto pasar. La hora real aparece cuando el vuelo despega y el registro
# se queda en la pantalla unas horas, asi que 5 minutos sobra para no perder
# ninguna. Bajarlo no agrega informacion: castiga a un servidor ajeno.
INTERVALO_S = float(os.environ.get("AA2000_INTERVALO_S", 300))

AEROPUERTO = os.environ.get("AA2000_AEROPUERTO", "AEP").strip().upper()

DB_PATH = Path(os.environ.get("ADSB_OFICIAL")
               or (Path(os.environ.get("ADSB_DB") or ".").parent / "aa2000_oficial.db"))

TIEMPO_ESPERA_S = 45

SCHEMA = """
CREATE TABLE IF NOT EXISTS vuelo_oficial (
    id TEXT PRIMARY KEY,
    aeropuerto TEXT,
    movimiento TEXT,
    numero TEXT,
    aerolinea_id TEXT,
    aerolinea TEXT,
    otro_aeropuerto TEXT,
    destino_nombre TEXT,
    programada TEXT,
    programada_epoch REAL,
    estimada TEXT,
    real TEXT,
    real_epoch REAL,
    estado TEXT,
    matricula TEXT,
    pasajeros INTEGER,
    posicion TEXT,
    puerta TEXT,
    sector TEXT,
    cinta TEXT,
    checkins TEXT,
    rotacion TEXT,
    cuerpo TEXT,
    tipo_vuelo TEXT,
    primera_vez REAL,
    ultima_vez REAL,
    veces_visto INTEGER
);
CREATE INDEX IF NOT EXISTS idx_oficial_prog ON vuelo_oficial (programada_epoch);
CREATE INDEX IF NOT EXISTS idx_oficial_mov  ON vuelo_oficial (aeropuerto, movimiento);
"""

# Los campos que NUNCA se pisan con vacio. El feed es una pantalla: un vuelo
# puede volver con menos datos que la vez anterior -- se va del borde de la
# ventana, o el registro se reemplaza por uno mas nuevo del mismo numero -- y
# sobrescribir una hora real ya vista con "" seria perder el unico dato que
# este modulo existe para capturar.
NO_DEGRADAR = ("real", "real_epoch", "matricula", "pasajeros", "estado",
               "posicion", "puerta", "cinta")


def _pedir(endpoint: str, **params) -> list | dict:
    """GET al gateway, con la cabecera que exige."""
    url = f"{BASE}/{endpoint}?" + urllib.parse.urlencode(params)
    pedido = urllib.request.Request(url, headers={
        "Origin": ORIGIN,
        "Accept": "application/json",
        # Un User-Agent identificable: con el de urllib el gateway contesta
        # igual, pero decir quien golpea es lo honesto en un servidor ajeno.
        "User-Agent": "Mozilla/5.0 (compatible; radar-ypf/1.0)",
    })
    with urllib.request.urlopen(pedido, timeout=TIEMPO_ESPERA_S) as r:
        return json.loads(r.read().decode("utf-8", "replace"))


def _texto(fila: dict, clave: str) -> str | None:
    v = fila.get(clave)
    if v is None:
        return None
    v = str(v).strip()
    return v or None


def _pasajeros(fila: dict) -> int | None:
    """El conteo de pasajeros, distinguiendo CERO de NO INFORMADO.

    No es una sutileza: medido el 2026-09-06, de 500 registros 114 traian
    pasajeros, y entre los que despegaron habia AR 1531 con "0" y AR 1857 con
    el campo vacio. Un cero ahi es casi seguro "no informado todavia" y no
    "volo vacio", pero los dos casos EXISTEN en el feed y hay que poder
    separarlos. Guardar 0 para los dos haria imposible saber despues cual era
    cual.
    """
    v = fila.get("pasajeros")
    if v is None or str(v).strip() == "":
        return None
    try:
        return int(str(v).strip())
    except ValueError:
        return None


def _resolver_epoch(marca: str | None, ahora: float) -> float | None:
    """"07/09 09:15" -> epoch. El feed NO manda el ano.

    Es el unico lugar donde este modulo puede equivocarse en silencio, asi que
    se resuelve contra la ventana real del feed en vez de asumir el ano actual:
    la ventana va de "ahora" a unas 26 h adelante, mas unas horas del pasado
    reciente que sigue en pantalla. Se prueban el ano de ahora y sus vecinos, y
    se elige el que caiga MAS CERCA de ahora.

    Sin esto, un sondeo del 31 de diciembre a las 23:50 leeria "01/01 00:30"
    como enero del ano que termina -once meses y medio en el pasado- y la fila
    quedaria fechada un ano antes para siempre.
    """
    if not marca:
        return None
    partes = marca.strip().split()
    if len(partes) < 2:
        return None
    dia_mes, hora = partes[0], partes[1]
    try:
        d, m = (int(x) for x in dia_mes.split("/")[:2])
        hms = [int(x) for x in hora.split(":")]
    except ValueError:
        return None
    hms += [0] * (3 - len(hms))
    base = datetime.fromtimestamp(ahora, timezone.utc)
    mejor = None
    for delta in (-1, 0, 1):
        try:
            cand = datetime(base.year + delta, m, d, hms[0], hms[1], hms[2],
                            tzinfo=timezone.utc).timestamp()
        except ValueError:
            continue            # 29/02 en un ano que no es bisiesto
        if mejor is None or abs(cand - ahora) < abs(mejor - ahora):
            mejor = cand
    return mejor


def _a_fila(cruda: dict, aeropuerto: str, movimiento: str, ahora: float) -> dict | None:
    ident = _texto(cruda, "id")
    if not ident:
        return None
    prog = _texto(cruda, "stda")
    real = _texto(cruda, "atda")
    return {
        "id": ident,
        # EL AEROPUERTO SALE DE LA FILA, NO DE LO QUE PEDIMOS. Medido el
        # 2026-09-07: id_arpt NO FILTRA NADA -- pedir AEP, EZE o COR devuelve los
        # mismos 60 ids byte por byte-. all-flights es un feed NACIONAL, asi que
        # guardar el aeropuerto consultado etiquetaba como "de Aeroparque"
        # partidas de todo el pais y el denominador de cualquier share quedaba
        # inflado. El filtro por aeropuerto lo hace sondear(), sobre `arpt`.
        "aeropuerto": _texto(cruda, "arpt") or aeropuerto,
        # El movimiento del CONSULTADO y no el campo "mov" del registro: movtp SI
        # funciona -- D y A devuelven conjuntos distintos-- y es lo que pedimos.
        "movimiento": movimiento,
        "numero": _texto(cruda, "nro"),
        "aerolinea_id": _texto(cruda, "idaerolinea"),
        "aerolinea": _texto(cruda, "aerolinea"),
        # EL OTRO EXTREMO DE LA RUTA, y antes esto estaba al reves. `arpt` es el
        # ORIGEN de la partida, no el destino: verificado con vuelos de origen
        # conocido -- IB 0102 (Iberia) trae arpt=EZE y sale de Ezeiza, UX 122
        # (Air Europa) trae arpt=COR y sale de Cordoba-. El destino esta en
        # IATAdestorig: IB 0102 -> MAD, BA 248 -> LHR, AZ 681 -> FCO.
        #
        # Guardando `arpt` aca la ruta salia "AEP -> AEP", que no existe y era la
        # senal de que el campo estaba mal leido.
        #
        # Para una partida la ruta es aeropuerto -> otro_aeropuerto; para un
        # arribo es al reves.
        "otro_aeropuerto": _texto(cruda, "IATAdestorig"),
        "destino_nombre": _texto(cruda, "destorig"),
        "programada": prog,
        "programada_epoch": _resolver_epoch(prog, ahora),
        "estimada": _texto(cruda, "etda"),
        "real": real,
        "real_epoch": _resolver_epoch(real, ahora),
        "estado": _texto(cruda, "estes"),
        "matricula": _texto(cruda, "matricula"),
        "pasajeros": _pasajeros(cruda),
        "posicion": _texto(cruda, "posicion"),
        "puerta": _texto(cruda, "gate"),
        "sector": _texto(cruda, "sector"),
        "cinta": _texto(cruda, "belt"),
        "checkins": _texto(cruda, "checkins"),
        "rotacion": _texto(cruda, "rot"),
        "cuerpo": _texto(cruda, "acft_body"),
        "tipo_vuelo": _texto(cruda, "id_flight_tp"),
    }


def abrir(db_path: Path | str = DB_PATH) -> sqlite3.Connection:
    db_path = Path(db_path)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    # WAL igual que adsb_log: deja leer mientras el poller escribe, que es lo
    # que necesita la pagina.
    conn.execute("PRAGMA journal_mode=WAL")
    conn.executescript(SCHEMA)
    conn.commit()
    return conn


def abrir_lectura(db_path: Path | str = DB_PATH) -> sqlite3.Connection | None:
    """Solo lectura, para la webapp. None si todavia no hay base.

    Separado de abrir() y no un parametro porque abrir() ejecuta el SCHEMA, o
    sea que ESCRIBE: si la pagina lo llamara, una consulta a un tablero crearia
    la base vacia y el "todavia no hay datos" se volveria indistinguible de
    "el poller nunca corrio". Y en modo ro no puede tocar lo que el poller
    escribe, que es exactamente la garantia que hace falta.
    """
    db_path = Path(db_path)
    if not db_path.exists():
        return None
    conn = sqlite3.connect(f"file:{db_path.as_posix()}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def guardar(conn: sqlite3.Connection, filas: list[dict], ahora: float | None = None) -> dict:
    """Insertar o actualizar, sin degradar lo ya visto. Devuelve el conteo."""
    ahora = time.time() if ahora is None else ahora
    nuevas = actualizadas = sin_cambio = 0
    for f in filas:
        previa = conn.execute("SELECT * FROM vuelo_oficial WHERE id = ?",
                              (f["id"],)).fetchone()
        if previa is None:
            f = dict(f, primera_vez=ahora, ultima_vez=ahora, veces_visto=1)
            conn.execute(f"INSERT INTO vuelo_oficial ({','.join(f)}) VALUES "
                         f"({','.join('?' * len(f))})", tuple(f.values()))
            nuevas += 1
            continue
        cambios = {}
        for k, v in f.items():
            anterior = previa[k]
            # La regla que hace que esto sirva: lo que ya se sabe no se pierde
            # porque el feed lo dejo de mandar. Ver NO_DEGRADAR.
            if v is None and k in NO_DEGRADAR and anterior is not None:
                continue
            if v != anterior:
                cambios[k] = v
        cambios["ultima_vez"] = ahora
        cambios["veces_visto"] = (previa["veces_visto"] or 0) + 1
        conn.execute("UPDATE vuelo_oficial SET "
                     + ",".join(f"{k}=?" for k in cambios)
                     + " WHERE id = ?", tuple(cambios.values()) + (f["id"],))
        # Dos claves siempre cambian (ultima_vez, veces_visto): si no hay mas,
        # la fila no aporto nada nuevo.
        if len(cambios) > 2:
            actualizadas += 1
        else:
            sin_cambio += 1
    conn.commit()
    return {"nuevas": nuevas, "actualizadas": actualizadas, "sin_cambio": sin_cambio}


def sondear(conn: sqlite3.Connection, aeropuerto: str | None = AEROPUERTO,
            movimientos: tuple[str, ...] = ("D", "A"), log=print) -> dict:
    """Una pasada por los dos movimientos. Nada se descarta en silencio.

    `aeropuerto=None` guarda las partidas de TODO EL PAIS, que es el universo
    del market share: el interior es YPF por la regla de negocio, sin planilla.
    Con un codigo puesto se filtra a ese aeropuerto, que es lo que necesitan
    /aeropuerto y el cruce con el ADS-B.
    """
    ahora = time.time()
    total = {"nuevas": 0, "actualizadas": 0, "sin_cambio": 0,
             "recibidas": 0, "sin_id": 0, "con_real": 0,
             "de_otro_aeropuerto": 0, "errores": []}
    for mov in movimientos:
        try:
            # id_arpt NO FILTRA del lado del servidor (ver _a_fila), asi que
            # mandarlo o no da la misma respuesta. Se omite cuando no hay
            # aeropuerto para no enviar "id_arpt=None" literal en la URL.
            params = {"movtp": mov, "c": CANTIDAD}
            if aeropuerto:
                params["id_arpt"] = aeropuerto
            crudas = _pedir("all-flights", **params)
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, OSError) as exc:
            # Se anota y se sigue con el otro movimiento: perder una pasada de
            # partidas no tiene por que costar tambien la de arribos.
            total["errores"].append(f"{mov}: {type(exc).__name__}: {exc}")
            log(f"  {mov}: FALLO {type(exc).__name__}: {exc}")
            continue
        if not isinstance(crudas, list):
            total["errores"].append(f"{mov}: respuesta inesperada {type(crudas).__name__}")
            continue
        total["recibidas"] += len(crudas)
        filas = []
        for c in crudas:
            f = _a_fila(c, aeropuerto, mov, ahora)
            if f is None:
                total["sin_id"] += 1
                continue
            # EL FILTRO POR AEROPUERTO LO HACEMOS NOSOTROS. id_arpt no filtra
            # (ver _a_fila), asi que sin esto entrarian las partidas de todo el
            # pais. Se descartan y SE CUENTAN: un numero de partidas que incluye
            # Ezeiza y Cordoba no es el de Aeroparque, y no decir cuantas se
            # dejaron afuera haria imposible notar que el filtro cambio.
            if aeropuerto and f["aeropuerto"] != aeropuerto:
                total["de_otro_aeropuerto"] += 1
                continue
            if f["real"]:
                total["con_real"] += 1
            filas.append(f)
        r = guardar(conn, filas, ahora)
        for k in ("nuevas", "actualizadas", "sin_cambio"):
            total[k] += r[k]
        log(f"  {mov}: {len(crudas)} recibidas, {len(filas)} de "
            f"{aeropuerto or 'todo el pais'}, "
            f"{r['nuevas']} nuevas, {r['actualizadas']} actualizadas")
    return total


def resumen(conn: sqlite3.Connection,
            aeropuerto: str | None = AEROPUERTO) -> dict:
    """Que hay acumulado, con los ceros distinguidos de los vacios.

    `aeropuerto=None` cuenta TODO EL PAIS, igual que operaciones(). El filtro se
    arma una sola vez en `filtro` y se pega en cada consulta: repetir
    "WHERE aeropuerto=?" a mano en nueve consultas es como una queda sin el
    cambio y el resumen mezcla dos universos sin avisar.
    """
    filtro = " AND aeropuerto=?" if aeropuerto else ""
    pre = (aeropuerto,) if aeropuerto else ()

    def uno(sql, *a):
        return conn.execute(sql, a).fetchone()[0]
    base = "SELECT COUNT(*) FROM vuelo_oficial WHERE 1=1" + filtro
    d = {"aeropuerto": aeropuerto or "TODOS"}
    d["total"] = uno(base, *pre)
    for mov, nombre in (("D", "partidas"), ("A", "arribos")):
        d[nombre] = uno(base + " AND movimiento=?", *pre, mov)
        d[f"{nombre}_con_real"] = uno(
            base + " AND movimiento=? AND real IS NOT NULL", *pre, mov)
    d["con_matricula"] = uno(base + " AND matricula IS NOT NULL", *pre)
    # Cero y vacio se cuentan APARTE: ver _pasajeros().
    d["con_pasajeros"] = uno(
        base + " AND pasajeros IS NOT NULL AND pasajeros > 0", *pre)
    d["pasajeros_en_cero"] = uno(base + " AND pasajeros = 0", *pre)
    d["suma_pasajeros"] = uno(
        "SELECT COALESCE(SUM(pasajeros),0) FROM vuelo_oficial WHERE 1=1"
        + filtro + " AND movimiento='D'", *pre)
    fila = conn.execute(
        "SELECT MIN(programada_epoch), MAX(programada_epoch), MIN(primera_vez), "
        "MAX(ultima_vez) FROM vuelo_oficial WHERE 1=1" + filtro,
        pre).fetchone()
    d["desde_epoch"], d["hasta_epoch"] = fila[0], fila[1]
    d["primer_sondeo"], d["ultimo_sondeo"] = fila[2], fila[3]
    return d


def operaciones(conn: sqlite3.Connection, aeropuerto: str | None = AEROPUERTO,
                movimiento: str | None = None, solo_reales: bool = False,
                limite: int = 500) -> list[dict]:
    """Las filas acumuladas, la mas reciente primero.

    Se ordena por programada_epoch y NO por real_epoch: los que todavia no
    despegaron no tienen hora real, y ordenar por ella los mandaria al fondo,
    justo los que interesa ver arriba en una pantalla en vivo.

    `aeropuerto=None` NO FILTRA: devuelve las de TODO EL PAIS. Es lo que necesita
    el market share, porque la regla de negocio se resuelve por el ORIGEN y las
    partidas del interior son de YPF sin consultar la planilla. Para /aeropuerto
    y para el cruce con el ADS-B sigue haciendo falta filtrar por AEP, asi que
    esto es OPCIONAL y el default no cambia.
    """
    sql = "SELECT * FROM vuelo_oficial WHERE 1=1"
    args: list = []
    if aeropuerto:
        sql += " AND aeropuerto = ?"
        args.append(aeropuerto)
    if movimiento:
        sql += " AND movimiento = ?"
        args.append(movimiento)
    if solo_reales:
        sql += " AND real IS NOT NULL"
    sql += " ORDER BY programada_epoch DESC LIMIT ?"
    args.append(int(limite))
    return [dict(r) for r in conn.execute(sql, args)]


def _informe(r: dict) -> None:
    def hora(e):
        return "-" if not e else datetime.fromtimestamp(e, timezone.utc).strftime("%d/%m %H:%M")
    print(f"\nAcumulado de {r['aeropuerto']}")
    print(f"  vuelos            {r['total']}")
    print(f"  partidas          {r['partidas']}  ({r['partidas_con_real']} con hora real)")
    print(f"  arribos           {r['arribos']}  ({r['arribos_con_real']} con hora real)")
    print(f"  con matricula     {r['con_matricula']}")
    print(f"  con pasajeros     {r['con_pasajeros']}"
          f"   (+{r['pasajeros_en_cero']} informados en cero)")
    print(f"  ventana horaria   {hora(r['desde_epoch'])}  ..  {hora(r['hasta_epoch'])}")
    print(f"  sondeos           {hora(r['primer_sondeo'])}  ..  {hora(r['ultimo_sondeo'])}")


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--sondear", action="store_true", help="una pasada y salir")
    p.add_argument("--seguir", action="store_true",
                   help=f"quedarse sondeando cada {INTERVALO_S:.0f} s")
    p.add_argument("--resumen", action="store_true", help="que hay acumulado")
    p.add_argument("--aeropuerto", default=AEROPUERTO,
                   help="codigo IATA, o TODOS para el feed nacional completo")
    p.add_argument("--db", default=str(DB_PATH))
    a = p.parse_args()
    if not (a.sondear or a.seguir or a.resumen):
        p.error("hace falta --sondear, --seguir o --resumen")
    # TODOS es el universo del market share: la regla de negocio se resuelve por
    # el ORIGEN, asi que las partidas del interior son de YPF sin planilla y
    # dejarlas afuera tiraria justo las que se saben con certeza. Se escribe
    # TODOS y no una cadena vacia porque en la linea de comandos un valor vacio
    # se confunde con haberse olvidado de pasarlo.
    if (a.aeropuerto or "").strip().upper() in ("TODOS", "TODO", "PAIS"):
        a.aeropuerto = None

    conn = abrir(a.db)
    print(f"base: {a.db}")
    try:
        if a.resumen:
            _informe(resumen(conn, a.aeropuerto))
            return 0
        while True:
            marca = datetime.now(timezone.utc).strftime("%H:%M:%S")
            # Sin el 'or', con --aeropuerto TODOS el log decia "sondeando None".
            # Un log que no nombra el universo del que se conto no sirve para
            # auditar el numero despues.
            print(f"[{marca}] sondeando "
                  f"{a.aeropuerto or 'TODOS (todo el pais)'} ...")
            t = sondear(conn, a.aeropuerto)
            print(f"           {t['recibidas']} recibidas, {t['nuevas']} nuevas, "
                  f"{t['actualizadas']} actualizadas, {t['con_real']} con hora real"
                  + (f", {len(t['errores'])} FALLOS" if t["errores"] else ""))
            if not a.seguir:
                _informe(resumen(conn, a.aeropuerto))
                return 0
            time.sleep(INTERVALO_S)
    except KeyboardInterrupt:
        print("\ncortado a mano")
        _informe(resumen(conn, a.aeropuerto))
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    sys.exit(main())
