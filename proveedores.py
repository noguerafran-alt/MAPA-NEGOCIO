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
    vacia = {"existe": False, "rutas": {}, "por_aerolinea": {},
             "actualizado": None, "fuente": None, "error": None}
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
    # EXCEPCIONES POR AEROLINEA. La regla general sigue siendo que el proveedor es
    # de la RUTA -- la planilla de YPF no trae columna de aerolinea y su llave es
    # (PARTIDA, ARRIBO). Pero en Ezeiza aparecieron rutas donde una compania no la
    # abastece la misma petrolera que al resto: EZE-MAD la vuelan AR, IB, PU y UX, y
    # no tienen por que compartir contrato. Eso NO se puede expresar con una tabla
    # por ruta, y forzarlo obliga a elegir entre mentir sobre una aerolinea o dejar
    # la ruta entera sin declarar.
    #
    # Se modela como EXCEPCION y no como tabla paralela a proposito: la ruta sigue
    # teniendo su proveedor, y esto solo dice quien se aparta. Asi lo declarado sigue
    # siendo poco y cada excepcion es una afirmacion explicita de alguien, en vez de
    # 253 casillas que hay que mantener llenas.
    por_aerolinea = {}
    for k, v in (d.get("por_aerolinea") or {}).items():
        if "-" not in str(k) or not isinstance(v, dict):
            continue
        o, _, dst = str(k).partition("-")
        if not (_iata(o) and _iata(dst)):
            continue
        for aero, prov in v.items():
            if _iata(aero) and _iata(prov):
                por_aerolinea[(_iata(o), _iata(dst), _iata(aero))] = _iata(prov)
    return {"existe": True, "rutas": rutas, "por_aerolinea": por_aerolinea,
            "error": None,
            "actualizado": d.get("actualizado"), "fuente": d.get("fuente")}


def quien_cargo(origen, destino, tabla: dict, aerolinea=None) -> dict:
    """Quien abastecio esa partida: {proveedor, motivo}.

    `proveedor` es None cuando no se puede afirmar, y `motivo` dice por que.
    Se devuelven los dos juntos a proposito: un proveedor sin el motivo deja
    indistinguible "YPF porque es el interior" de "YPF porque lo dice la
    planilla", y son dos niveles de evidencia distintos.

    `aerolinea` es OPCIONAL y solo cambia algo si alguien declaro una excepcion
    para ese par (ruta, aerolinea). Es opcional y no obligatorio porque los
    llamadores viejos -- el market share del mapa, que agrega por ruta -- no
    tienen la aerolinea a mano, y ahi la respuesta correcta sigue siendo la de la
    ruta. Pasarla nunca empeora la respuesta: como mucho, la precisa.
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
    # La excepcion gana sobre la ruta: es mas especifica y la puso alguien a mano
    # sabiendo que esa compania se aparta. Motivo propio para que la pantalla pueda
    # distinguirla -- "YPF porque lo dice la ruta" y "Axion porque alguien declaro
    # que esta aerolinea no" son evidencias distintas, igual que interior y planilla.
    a = _iata(aerolinea)
    if a:
        p = (tabla.get("por_aerolinea") or {}).get((o, dst, a))
        if p:
            return {"proveedor": p, "motivo": "planilla_aerolinea"}
    p = tabla["rutas"].get((o, dst))
    if p:
        return {"proveedor": p, "motivo": "planilla"}
    # Ruta competitiva que la planilla no declara. No se supone nada.
    return {"proveedor": None, "motivo": "ruta_no_declarada"}
