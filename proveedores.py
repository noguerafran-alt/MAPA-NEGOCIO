"""Quien le cargo combustible a cada avion, por ORIGEN de la partida.

LA REGLA DE NEGOCIO, confirmada por Fran el 2026-09-07: EL AVION CARGA DONDE
DESPEGA. De ahi salen dos casos y nada mas:

  origen AEP, EZE o COR   hay competencia -> el proveedor lo dice la planilla
  cualquier otro del pais  YPF por definicion: no hay competencia ahi

SE CLASIFICA POR EL ORIGEN, NO POR EL PAR DE CIUDADES. Un AEP->BRC carga en
Aeroparque y es competitivo; el BRC->AEP de vuelta carga en Bariloche y es de
YPF. Mismo par, dos respuestas opuestas. Cualquier cosa que agrupe "la ruta
BRC-AEP" sin distinguir el sentido va a estar mal en la mitad de los casos.

EL PROVEEDOR ES DE LA RUTA DIRIGIDA, NO DE LA AEROLINEA. Es el cambio de modelo
respecto de `ypf_clientes.json` del repo del radar, que clasificaba por
aerolinea con rutas parciales adentro. La planilla de YPF no trae columna de
aerolinea: la llave es (PARTIDA, ARRIBO). Eso afirma que si dos aerolineas hacen
AEP->BRC, las dos cargan con el mismo proveedor.

LO NO DECLARADO NO ES DE NADIE. Una partida de AEP/EZE/COR cuya ruta no figura
en la planilla queda con proveedor None y motivo `ruta_no_declarada`. No se
reparte, no se supone YPF y no se supone competencia: se cuenta y se muestra.
Es la misma regla que ya rige en este proyecto -- desconocido no es competencia,
y un numero lindo sin respaldo es peor que un hueco explicado.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

# Los tres donde YPF tiene competencia. Fuera de estos, en Argentina, YPF es el
# unico que abastece, asi que el proveedor no necesita planilla.
COMPETITIVOS = frozenset({"AEP", "EZE", "COR"})

TABLA_PATH = Path(os.environ.get("YPF_PROVEEDORES")
                  or (Path(__file__).parent / "proveedores.json"))


def _iata(v) -> str:
    return str(v or "").strip().upper()


def cargar_tabla(ruta: Path | str = TABLA_PATH) -> dict:
    """La planilla de YPF ya convertida, o una vacia que lo dice.

    Nunca lanza. Que la tabla falte es un estado NORMAL: la aporta YPF y la
    convierte `cargar_excel.py` en la PC donde esta el Excel. Tiene que poder
    mostrarse como "falta el dato" en vez de romper la pagina.
    """
    ruta = Path(ruta)
    vacia = {"existe": False, "rutas": {}, "actualizado": None,
             "fuente": None, "error": None}
    if not ruta.exists():
        return vacia
    try:
        d = json.loads(ruta.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        return dict(vacia, error=f"no se pudo leer {ruta.name}: {exc}")
    # Las llaves viajan como "AEP-BRC" porque JSON no tiene tuplas. Se
    # normaliza a mayusculas al leer: la planilla puede traer "aep" y AA2000
    # publica "AEP", y comparar sin normalizar daria cero coincidencias -- o
    # sea, todas las rutas "no declaradas" y ningun proveedor. Falla ruidosa
    # de las que no se notan.
    rutas = {}
    for k, v in (d.get("rutas") or {}).items():
        if "-" not in str(k):
            continue
        o, _, dst = str(k).partition("-")
        if _iata(o) and _iata(dst) and _iata(v):
            rutas[(_iata(o), _iata(dst))] = _iata(v)
    return {"existe": True, "rutas": rutas, "error": None,
            "actualizado": d.get("actualizado"), "fuente": d.get("fuente")}


def quien_cargo(origen, destino, tabla: dict) -> dict:
    """Quien abastecio esa partida: {proveedor, motivo}.

    `proveedor` es None cuando no se puede afirmar, y `motivo` dice por que.
    Se devuelven los dos juntos a proposito: un proveedor sin el motivo deja
    indistinguible "YPF porque es el interior" de "YPF porque lo dice la
    planilla", y son dos niveles de evidencia distintos.
    """
    o, dst = _iata(origen), _iata(destino)
    if not o:
        return {"proveedor": None, "motivo": "sin_origen"}
    # El interior NO consulta la planilla, y por eso el universo de este
    # analisis es NACIONAL y no Aeroparque: las partidas del interior se
    # resuelven sin mirar el Excel. Vale porque el feed de partidas de AA2000
    # solo trae aeropuertos argentinos, asi que "no es AEP/EZE/COR" implica
    # "es del interior del pais" y no "es del exterior".
    if o not in COMPETITIVOS:
        return {"proveedor": "YPF", "motivo": "interior"}
    if not tabla.get("existe"):
        return {"proveedor": None, "motivo": "sin_planilla"}
    p = tabla["rutas"].get((o, dst))
    if p:
        return {"proveedor": p, "motivo": "planilla"}
    # Ruta competitiva que la planilla no declara. No se supone nada.
    return {"proveedor": None, "motivo": "ruta_no_declarada"}
