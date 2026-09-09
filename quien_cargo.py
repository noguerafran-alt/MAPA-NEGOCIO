"""Quien le cargo combustible a cada avion que despego, vuelo por vuelo.

Es la pregunta del negocio, y la unidad de la respuesta es EL VUELO: una fila
por partida, con el proveedor y CON EL MOTIVO por el que se afirma. El agregado
-- cuantas de cada proveedor -- sale abajo, pero es un resumen de estas filas y
no una cuenta aparte.

SOLO PARTIDAS. El avion carga antes de irse, asi que la operacion que importa es
el despegue. Un arribo no es una carga de combustible en el aeropuerto de
llegada, y meterlo mediria otra cosa.

SOLO LAS QUE OCURRIERON. Una partida programada que todavia no despego no es una
carga, y contarla haria que el resultado cambie segun la hora del dia en que se
mire la pantalla. Se cuentan aparte y se dicen.

EL UNIVERSO ES NACIONAL, no Aeroparque. Es consecuencia directa de la regla de
negocio: como se clasifica por el ORIGEN, las partidas del interior se resuelven
sin planilla, y dejarlas afuera tiraria justo las que se saben con certeza. Por
eso se lee con `aeropuerto=None`.

NO DEPENDE DE LA ANTENA. Todo sale del feed oficial de AA2000, asi que la
cobertura del ADS-B no entra en esta cuenta. Es lo que lo hace publicable hoy,
a diferencia de los porcentajes que este proyecto ya publico mal.

CADA FILA DICE SU MOTIVO, y son cuatro:

    interior            YPF por la regla de negocio: no hay competencia ahi
    planilla            lo declara el Excel de YPF para esa ruta dirigida
    ruta_no_declarada   parte de AEP/EZE/COR y la planilla no dice nada
    sin_planilla        no hay proveedores.json todavia

Los dos ultimos NO son YPF ni competencia: son huecos, y se muestran como
huecos. Repartirlos seria el numero lindo sin respaldo que CLAUDE.md prohibe.

Uso:
  python quien_cargo.py                     todo lo acumulado
  python quien_cargo.py --horas 24          ultimas 24 h
  python quien_cargo.py --origen AEP        solo las que salen de Aeroparque
  python quien_cargo.py --csv salida.csv    para analizar en otra herramienta
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import io
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import aa2000
import proveedores


# Los unicos estados del feed que AFIRMAN que el vuelo no salio. Todo lo demas sin
# hora real no es "no salio": es "no sabemos". Medido el 2026-09-08 sobre la base del
# radar: de 252 partidas pasadas sin hora real, 246 estaban congeladas en estados que
# significan que estaba por salir ("En Horario" 204, "Cerrado" 13, "Pre Embarque" 11,
# "Embarcando" 8, "Ultimo aviso" 4) y solo 6 eran cancelaciones reales.
ESTADOS_NO_SALIO = ("cancelado", "no opera")


def piso_de_medicion(carpeta=None):
    """Desde cuando el market share es confiable, en epoch. None si no hay piso.

    Existe porque el periodo viejo esta ROTO, no incompleto. Hasta el 2026-09-08 nadie
    sondeaba de forma continua: el mapa le cedia el turno al radar por el solo hecho de
    estar instalado, y con el radar apagado no se capturaba ninguna hora de despegue.
    De 677 partidas, 173 tenian hora y 246 se habian ido sin que nadie las viera salir.

    Un porcentaje sobre ese tramo no es "una muestra mas chica": esta SESGADO hacia los
    ratos en que alguien casualmente estaba sondeando, y no hay forma de saber si esos
    ratos se parecen al resto del dia. Por eso el tramo se descarta entero en vez de
    arrastrarlo con un asterisco.

    RECIBE LA CARPETA, no la adivina. Este modulo se carga desde dos lugares distintos
    -- la copia del radar y la de este repo -- y `aa2000.DB_PATH` apunta a la carpeta de
    la copia que se cargo. Derivar el sello de ahi lo hacia buscar en un lugar donde no
    estaba, y el piso se ignoraba en silencio: el market share seguia contando el tramo
    viejo como si nada. El sello es UNO por maquina, asi que lo pasa quien llama.
    """
    if carpeta is None:
        return None
    try:
        with io.open(os.path.join(str(carpeta), "medicion_desde.json"),
                     encoding="utf-8") as fh:
            return float(json.load(fh)["epoch"])
    except Exception:
        return None


def estado_partida(p, ahora=None):
    """despego | no_salio | sin_dato | programada. Cuatro y no dos, a proposito.

    El bug que motivo esto: la pantalla mostraba "-" bajo la columna "Despego" y
    contaba esas filas como "sin despegar". Para una partida cuya hora ya paso eso es
    una AFIRMACION FALSA -- el avion salio, lo que falto fue mirar. La hora real solo
    se captura si un sondeo cae en la ventana en que el feed la publica, y si nadie
    sondea (el mapa cede el turno al radar, el radar esta apagado) no se captura nunca.

    `sin_dato` no se reparte ni se cuenta como carga: es un hueco y se muestra como
    hueco, igual que `ruta_no_declarada`. Los porcentajes no cambian -- siguen saliendo
    solo de las que tienen hora real. Lo que cambia es que la pantalla deja de decir
    que no despegaron.
    """
    if p.get("real_epoch"):
        return "despego"
    if (p.get("estado") or "").strip().lower() in ESTADOS_NO_SALIO:
        return "no_salio"
    prog = p.get("programada_epoch")
    if prog is None:
        return "sin_dato"
    if ahora is None:
        ahora = time.time()
    return "sin_dato" if prog < ahora else "programada"


def _hora(epoch) -> str:
    if not epoch:
        return "-"
    return datetime.fromtimestamp(epoch, timezone.utc).strftime("%d/%m %H:%M")


def _meta_tabla(tabla: dict) -> dict:
    """Los metadatos de la planilla, sin nada que no sea serializable a JSON.

    LISTA BLANCA Y NO NEGRA, y esto ya costo una pantalla en blanco. Antes esto era
    `{k: v for k, v in tabla.items() if k != "rutas"}`: excluia por nombre la unica
    clave con llaves de tupla que habia entonces. Al agregar `por_aerolinea` -- que
    tambien esta indexada por tuplas (origen, destino, aerolinea) -- la exclusion no
    la alcanzaba, `jsonify` tiraba "keys must be str, int, float, bool or None, not
    tuple" y la pagina entera quedaba vacia con un 500 que no decia nada del origen.

    Con lista blanca, agregar una estructura interna a la tabla no puede volver a
    romper la API: hay que pedir explicitamente que salga.
    """
    return {k: tabla.get(k) for k in ("existe", "actualizado", "fuente", "error")}


def por_vuelo(partidas: list[dict], tabla: dict) -> list[dict]:
    """Una fila por partida, con proveedor y motivo. Mas reciente primero."""
    filas = []
    for p in partidas:
        # La aerolinea va SIEMPRE, aunque casi nunca cambie nada: solo pesa si
        # alguien declaro una excepcion para ese par (ruta, aerolinea). Pasarla
        # aca y no en el llamador es lo que hace que la excepcion valga en las
        # tres vistas -- el tablero, los porcentajes y el desglose por ruta -- sin
        # que ninguna tenga que acordarse.
        q = proveedores.quien_cargo(p.get("aeropuerto"),
                                    p.get("otro_aeropuerto"), tabla,
                                    p.get("aerolinea_id"))
        filas.append({
            "numero": p.get("numero"),
            "aerolinea_id": p.get("aerolinea_id"),
            "aerolinea": p.get("aerolinea"),
            "origen": (p.get("aeropuerto") or "").upper(),
            "destino": (p.get("otro_aeropuerto") or "").upper(),
            "destino_nombre": p.get("destino_nombre"),
            "matricula": p.get("matricula"),
            "cuerpo": p.get("cuerpo"),
            # Dato para el modelo de consumo de YPF, NO parte de esta cuenta.
            # None cuando la fuente no lo informa, que es distinto de cero.
            "pasajeros": p.get("pasajeros"),
            "real_epoch": p.get("real_epoch"),
            # Para el tablero, que muestra tambien las que todavia no salieron:
            # el portal de AA2000 las lista y esconderlas seria una pantalla
            # menos util. En la CUENTA no entran -- ver desde_base().
            "programada": p.get("programada"),
            "programada_epoch": p.get("programada_epoch"),
            "estado": p.get("estado"),
            # `ocurrio` se conserva tal cual: es lo que define el denominador de los
            # porcentajes y esa cuenta no cambia. `situacion` es para la pantalla.
            "ocurrio": bool(p.get("real_epoch")),
            "situacion": estado_partida(p),
            "proveedor": q["proveedor"],
            "motivo": q["motivo"],
        })
    # PRIMERO LAS QUE YA DESPEGARON, la mas reciente arriba; despues las
    # programadas, la proxima en salir primero.
    #
    # Ordenar todo por hora descendente parecia lo natural y abria el tablero en
    # los vuelos de MANANA: para una partida futura, "la mas reciente" es la mas
    # lejana. Nadie abre un tablero para ver eso. Los dos grupos quieren orden
    # opuesto -- lo que paso se lee del ultimo al primero, y lo que viene se lee
    # del proximo al ultimo-- asi que van separados.
    #
    # Para desde_base() no cambia nada: ahi todas las filas ya despegaron.
    filas.sort(key=lambda x: (
        0 if x["ocurrio"] else 1,
        -(x["real_epoch"] or 0) if x["ocurrio"]
        else (x["programada_epoch"] or 0),
    ))
    return filas


def resumen(filas: list[dict]) -> dict:
    """Cuantas de cada proveedor, y los huecos contados por separado.

    Los huecos NO se reparten entre los proveedores y tampoco se sacan del
    total: el denominador tiene que seguir siendo todas las partidas, porque un
    porcentaje calculado sobre "las que pudimos clasificar" es el mismo defecto
    de denominador que ya hizo publicar mal dos veces en este proyecto.
    """
    por_prov: dict[str, int] = {}
    por_motivo: dict[str, int] = {}
    sin_resolver = []
    for f in filas:
        por_motivo[f["motivo"]] = por_motivo.get(f["motivo"], 0) + 1
        if f["proveedor"]:
            por_prov[f["proveedor"]] = por_prov.get(f["proveedor"], 0) + 1
        else:
            sin_resolver.append(f)
    # Las rutas que le faltan a la planilla, con cuantas partidas cuesta cada
    # una: es la lista para pedirle a YPF, ordenada por lo que mas conviene
    # completar primero.
    faltan: dict[str, int] = {}
    for f in sin_resolver:
        if f["motivo"] == "ruta_no_declarada":
            k = f"{f['origen']}-{f['destino']}"
            faltan[k] = faltan.get(k, 0) + 1
    # POR RUTA DIRIGIDA. El proveedor es funcion de (origen, destino), asi que
    # todas las partidas de una ruta comparten proveedor y motivo: la ruta es la
    # unidad natural de esta tabla. Se agrega ACA y no en la pagina para que la
    # consola y la web no calculen lo mismo por separado -- este repo ya sabe
    # como termina eso: dos acumuladores del cilindro dando numeros distintos y
    # nadie sabiendo cual creer.
    por_ruta: dict[str, dict] = {}
    for f in filas:
        k = f"{f['origen']}-{f['destino']}"
        d = por_ruta.setdefault(k, {
            "ruta": k, "origen": f["origen"], "destino": f["destino"],
            "destino_nombre": f["destino_nombre"],
            "proveedor": f["proveedor"], "motivo": f["motivo"],
            "vuelos": 0, "aerolineas": set(), "_por_aero": {}})
        d["vuelos"] += 1
        if f["aerolinea_id"]:
            a = f["aerolinea_id"].upper()
            d["aerolineas"].add(a)
            # El desglose que la pantalla necesita para poder abrir la ruta y
            # editar una aerolinea sola. Se arma aca, con las filas ya
            # clasificadas, para que diga exactamente lo mismo que los totales:
            # calcularlo en el navegador seria el segundo acumulador.
            e = d["_por_aero"].setdefault(a, {
                "aerolinea_id": a, "aerolinea": f.get("aerolinea"),
                "proveedor": f["proveedor"], "motivo": f["motivo"], "vuelos": 0})
            e["vuelos"] += 1
    rutas = sorted(por_ruta.values(), key=lambda d: -d["vuelos"])
    for d in rutas:
        # set no es serializable a JSON, y la pagina consume esto por la API.
        d["aerolineas"] = sorted(d["aerolineas"])
        d["por_aerolinea"] = sorted(d.pop("_por_aero").values(),
                                    key=lambda e: -e["vuelos"])
        # EL PROVEEDOR DE LA FILA ES EL DE LA RUTA, NO EL DEL PRIMER VUELO.
        # Se tomaba del primer vuelo de la ruta, y con las excepciones por aerolinea
        # eso empezo a mentir: si la primera partida de EZE-MAD es de la unica
        # compania declarada aparte, la ruta entera aparecia con el proveedor de esa
        # excepcion -- y el selector de la fila ofrecia pisar la ruta con el.
        # Se busca la primera partida que NO sea excepcion: esa responde por la ruta.
        propia = next((e for e in d["por_aerolinea"]
                       if e["motivo"] != "planilla_aerolinea"), None)
        if propia is not None:
            d["proveedor"] = propia["proveedor"]
            d["motivo"] = propia["motivo"]
        elif d["por_aerolinea"]:
            # Todas las companias de la ruta estan declaradas aparte. La ruta no tiene
            # respuesta propia observable, y decir la de una de ellas seria inventar
            # que vale para las demas.
            d["proveedor"] = None
            d["motivo"] = "solo_excepciones"
        # Si alguna aerolinea de la ruta tiene otro proveedor, la fila de la ruta
        # no puede mostrar uno solo como si valiera para todas. La pantalla lo usa
        # para marcarla en vez de mentir por omision.
        d["mixta"] = len({e["proveedor"] for e in d["por_aerolinea"]}) > 1

    # GUARDA DEL DENOMINADOR -- Y ACA NO ALCANZA, leer antes de confiar en ella.
    #
    # Cada fila va a por_prov o a sin_resolver, asi que la suma da `total` SIEMPRE:
    # este chequeo es una tautologia y no puede fallar nunca. Sirve como red contra un
    # cambio futuro en el reparto de filas, nada mas.
    #
    # LO QUE NO PUEDE VER es el caso real, porque no pasa aca: pasa cuando alguien
    # junta este resumen con el del tablero en un solo objeto y el `total` del tablero
    # -- que incluye las programadas -- pisa este. Desde adentro de resumen() eso es
    # invisible. El chequeo que si lo agarra esta en `_quien_cargo_plano()` de los dos
    # mapas, comparando el denominador contra las partidas con hora de despegue medida.
    #
    # No es una hipotesis: paso. Una version vieja de esta pantalla mostraba "209 de 714
    # partidas, 29,3% de YPF" con los porcentajes sumando 37,7% en vez de 100%, porque el
    # numerador eran las despegadas y el denominador incluia las 445 programadas. Un
    # porcentaje que no suma 100 y nadie lo dice es de las cosas que se leen como un
    # resultado y no como un error: el share de YPF quedaba a menos de la mitad.
    suma = sum(por_prov.values()) + len(sin_resolver)
    inconsistente = None
    if suma != len(filas):
        inconsistente = ("los proveedores suman %d y el total es %d: el denominador "
                         "incluye partidas sin clasificar" % (suma, len(filas)))

    return {
        "total": len(filas),
        # None cuando esta bien. La pagina lo muestra como aviso en vez de dibujar
        # porcentajes que no cierran.
        "denominador_inconsistente": inconsistente,
        "por_proveedor": dict(sorted(por_prov.items(), key=lambda kv: -kv[1])),
        "por_motivo": dict(sorted(por_motivo.items(), key=lambda kv: -kv[1])),
        "n_sin_resolver": len(sin_resolver),
        "rutas_sin_declarar": dict(sorted(faltan.items(),
                                          key=lambda kv: -kv[1])),
        "por_ruta": rutas,
        "n_rutas": len(rutas),
    }


def desde_base(db: str, horas: float | None = None,
               origen: str | None = None, tabla: dict | None = None,
               piso: float | None = None):
    """Lee las partidas ocurridas y las clasifica. None si falta la base."""
    conn = aa2000.abrir_lectura(db)
    if conn is None:
        return None
    try:
        # aeropuerto=None: TODO EL PAIS. Ver el docstring del modulo.
        crudas = aa2000.operaciones(conn, aeropuerto=None, movimiento="D",
                                    limite=200000)
    finally:
        conn.close()
    # SOLO LAS CONFIRMADAS: con hora de despegue MEDIDA. Nada de dar por despegado
    # lo que nadie vio salir.
    #
    # Se probo lo contrario -- incluir las "sin dato" para no perder muestra -- y se
    # descarto: meter 246 partidas que se SUPONEN despegadas dentro de un numero que
    # va a una planilla de negocio es exactamente el numero lindo sin respaldo que
    # CLAUDE.md prohibe. Mejor una muestra chica y verdadera.
    #
    # El problema que eso resolvia se resuelve por el otro lado: el piso de medicion.
    # Antes del 2026-09-08 nadie sondeaba de forma continua (el mapa le cedia el turno
    # al radar apagado), asi que las horas de despegue faltan de a ratos y el periodo
    # viejo esta sesgado hacia los momentos en que alguien miraba. Ese tramo se
    # descarta entero en vez de arrastrarlo: ver piso_de_medicion().
    ocurridas = [f for f in crudas if f.get("real_epoch")]
    programadas = sum(1 for f in crudas if estado_partida(f) == "programada")
    no_salieron = sum(1 for f in crudas if estado_partida(f) == "no_salio")

    descartadas_viejas = 0
    if piso:
        antes = len(ocurridas)
        ocurridas = [f for f in ocurridas if f["real_epoch"] >= piso]
        descartadas_viejas = antes - len(ocurridas)

    if horas:
        import time
        corte = time.time() - horas * 3600
        ocurridas = [f for f in ocurridas if f["real_epoch"] >= corte]
    if origen:
        o = origen.strip().upper()
        ocurridas = [f for f in ocurridas
                     if (f.get("aeropuerto") or "").upper() == o]
    tabla = tabla if tabla is not None else proveedores.cargar_tabla()
    filas = por_vuelo(ocurridas, tabla)
    r = resumen(filas)
    r["vuelos"] = filas
    r["tabla"] = _meta_tabla(tabla)
    r["n_rutas_en_tabla"] = len(tabla.get("rutas") or {})
    r["programadas_sin_ocurrir"] = programadas
    r["no_salieron"] = no_salieron
    # El piso y cuanto se dejo afuera por el: la pantalla tiene que poder decir
    # "medido desde tal fecha" en vez de dar un porcentaje sin periodo.
    r["piso_medicion"] = piso
    r["descartadas_por_piso"] = descartadas_viejas
    r["desde_epoch"] = min((f["real_epoch"] for f in filas), default=None)
    r["hasta_epoch"] = max((f["real_epoch"] for f in filas), default=None)
    return r


def tablero(db: str, horas: float | None = None, origen: str | None = None,
            tabla: dict | None = None):
    """El tablero de partidas, como el portal de AA2000 pero con proveedor.

    LA DIFERENCIA CON desde_base() ES QUE ACA ENTRAN LAS PROGRAMADAS. El portal
    las lista y son la mitad de para que se mira un tablero, asi que esconderlas
    seria una pantalla peor. Cada fila trae `ocurrio` y el tablero las distingue
    a la vista.

    Que aparezcan en la LISTA no las mete en la CUENTA: los porcentajes siguen
    saliendo de desde_base(), sobre las que ya despegaron. Una partida que
    todavia no salio no es una carga de combustible, y meterla haria que el
    numero cambie segun la hora del dia en que se mire la pantalla.

    SOLO PARTIDAS, igual que todo el resto de este modulo. Un arribo no es una
    carga en el aeropuerto de llegada: cargo en su origen, que para un arribo es
    `otro_aeropuerto` y no `aeropuerto` -- estan al reves. Y para los que vienen
    del exterior la regla del interior no aplica, porque "no es AEP/EZE/COR" ahi
    significa Madrid o Miami y no Bariloche. Por eso el tablero es de despegues.
    """
    conn = aa2000.abrir_lectura(db)
    if conn is None:
        return None
    try:
        crudas = aa2000.operaciones(conn, aeropuerto=None, movimiento="D",
                                    limite=200000)
    finally:
        conn.close()
    if horas:
        import time
        corte = time.time() - horas * 3600
        crudas = [f for f in crudas
                  if (f.get("real_epoch") or f.get("programada_epoch") or 0)
                  >= corte]
    if origen:
        o = origen.strip().upper()
        crudas = [f for f in crudas
                  if (f.get("aeropuerto") or "").upper() == o]
    tabla = tabla if tabla is not None else proveedores.cargar_tabla()
    filas = por_vuelo(crudas, tabla)
    return {
        "vuelos": filas,
        "total": len(filas),
        "ocurridas": sum(1 for f in filas if f["ocurrio"]),
        # Los tres huecos, separados: no es lo mismo "todavia no salio" (se resuelve
        # solo con el tiempo) que "no sabemos" (se resuelve sondeando) que "no salio"
        # (no hay nada que resolver).
        "sin_dato": sum(1 for f in filas if f["situacion"] == "sin_dato"),
        "no_salio": sum(1 for f in filas if f["situacion"] == "no_salio"),
        "programadas": sum(1 for f in filas if f["situacion"] == "programada"),
        "origenes": sorted({f["origen"] for f in filas if f["origen"]}),
        "tabla": _meta_tabla(tabla),
        "n_rutas_en_tabla": len(tabla.get("rutas") or {}),
    }


def informe(r, limite: int = 40) -> None:
    if r is None:
        print("falta la base de AA2000: corre el poller primero"
              f" (esperada en {aa2000.DB_PATH})")
        return
    t = r["tabla"]
    print("\nQuien le cargo a cada avion  (partidas ocurridas, todo el pais)")
    print(f"  ventana        {_hora(r['desde_epoch'])}  ..  "
          f"{_hora(r['hasta_epoch'])}")
    print(f"  partidas       {r['total']}"
          f"   (+{r['programadas_sin_ocurrir']} programadas sin despegar)")
    if not t["existe"]:
        # No se calla ni se muestra un cero: sin la planilla las partidas de
        # AEP/EZE/COR no se pueden atribuir, y hay que decir cuales faltan.
        print("\n  NO HAY PLANILLA DE PROVEEDORES (proveedores.json).")
        print("  Las partidas del interior igual son de YPF por la regla de")
        print("  negocio; las de AEP, EZE y COR quedan SIN RESOLVER.")
        if t.get("error"):
            print(f"  {t['error']}")
        print("  Para cargarla:  python cargar_excel.py planilla.xlsx")
    else:
        print(f"  planilla       {r['n_rutas_en_tabla']} rutas"
              f"   actualizada {t['actualizado'] or 'sin fecha'}")

    if r["total"]:
        print("\n  QUIEN CARGO")
        for prov, n in r["por_proveedor"].items():
            print(f"    {prov:12} {n:5}   {n / r['total'] * 100:5.1f}%")
        if r["n_sin_resolver"]:
            print(f"    {'SIN RESOLVER':12} {r['n_sin_resolver']:5}"
                  f"   {r['n_sin_resolver'] / r['total'] * 100:5.1f}%"
                  f"   -- no se reparten")
        print("\n  por que se afirma cada una:")
        for m, n in r["por_motivo"].items():
            print(f"    {m:20} {n:5}")

    if r["rutas_sin_declarar"]:
        print(f"\n  RUTAS QUE LE FALTAN A LA PLANILLA"
              f" ({len(r['rutas_sin_declarar'])}), por partidas perdidas:")
        for ruta, n in list(r["rutas_sin_declarar"].items())[:20]:
            print(f"    {ruta:10} {n:4} partidas")
        if len(r["rutas_sin_declarar"]) > 20:
            print(f"    ... y {len(r['rutas_sin_declarar']) - 20} mas")

    if r["vuelos"]:
        print(f"\n  VUELO POR VUELO  (las {min(limite, len(r['vuelos']))} mas"
              f" recientes de {len(r['vuelos'])})")
        print(f"  {'cuando':12} {'vuelo':10} {'ruta':9} {'proveedor':11}"
              f" {'matricula':9} motivo")
        for v in r["vuelos"][:limite]:
            print(f"  {_hora(v['real_epoch']):12} {(v['numero'] or '-'):10} "
                  f"{v['origen']}-{v['destino']:5} "
                  f"{(v['proveedor'] or '?'):11} "
                  f"{(v['matricula'] or '-'):9} {v['motivo']}")


ENCABEZADO_CSV = ["fecha_utc", "numero", "aerolinea", "origen", "destino",
                  "proveedor", "motivo", "matricula", "pasajeros"]


def filas_csv(r):
    """Las filas del volcado, sin decidir a donde van.

    Existe separado de a_csv() porque el mismo volcado se sirve por HTTP desde
    /quien-cargo.csv: si cada uno armara sus columnas, la descarga de la pantalla
    y la del CLI se irian separando sin que nadie se entere.
    """
    for v in r["vuelos"]:
        yield [
            "" if not v["real_epoch"] else datetime.fromtimestamp(
                v["real_epoch"], timezone.utc).strftime("%Y-%m-%d %H:%M"),
            v["numero"] or "", v["aerolinea_id"] or "",
            v["origen"], v["destino"],
            v["proveedor"] or "", v["motivo"],
            v["matricula"] or "",
            "" if v["pasajeros"] is None else v["pasajeros"],
        ]


def escribir_csv(r, fh) -> None:
    """Escribe el volcado en un archivo ya abierto (o un StringIO).

    Separador `;` y BOM: Excel en configuracion regional espanola abre el CSV de
    comas en una sola columna. Y la fecha va como texto AAAA-MM-DD HH:MM y no
    como epoch, que Excel mostraria como un entero de diez digitos.
    """
    w = csv.writer(fh, delimiter=";")
    w.writerow(ENCABEZADO_CSV)
    for fila in filas_csv(r):
        w.writerow(fila)


def a_csv(r, salida: Path) -> None:
    """Volcado plano para analizar en otra herramienta.

    Va el MOTIVO en su propia columna: sin el, una fila "YPF" del interior y una
    "YPF" declarada en la planilla quedan indistinguibles, y son dos niveles de
    evidencia distintos.
    """
    with salida.open("w", newline="", encoding="utf-8-sig") as fh:
        escribir_csv(r, fh)


def main() -> int:
    p = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--horas", type=float, default=None,
                   help="solo las ultimas N horas")
    p.add_argument("--origen", default=None,
                   help="filtrar por aeropuerto de partida, ej. AEP")
    p.add_argument("--db", default=str(aa2000.DB_PATH))
    p.add_argument("--csv", default=None, help="volcar a CSV")
    p.add_argument("--limite", type=int, default=40,
                   help="cuantos vuelos listar en pantalla")
    a = p.parse_args()

    r = desde_base(a.db, a.horas, a.origen)
    informe(r, a.limite)
    if r is None:
        return 1
    if a.csv:
        salida = Path(a.csv)
        a_csv(r, salida)
        print(f"\n  escrito {salida}  ({len(r['vuelos'])} vuelos)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
