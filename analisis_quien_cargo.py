# -*- coding: utf-8 -*-
"""Analisis de "Quien le cargo": los mismos vuelos, agrupados por AEROLINEA.

La pantalla /quien-cargo responde "que paso hoy, vuelo por vuelo". Esta responde
"como nos va con cada compania": su puntualidad, su ocupacion, cuantos m3 mueve y
que parte de esos m3 carga YPF.

NO RECALCULA NADA. `msrtic.filas()` ya cruza matricula -> tipo de avion real ->
consumo, ya separa cabotaje de internacional y ya clasifica el proveedor de cada
ruta; aca solo se agrupa distinto. Duplicar esa cuenta seria tener dos pantallas
del mismo sistema afirmando cosas distintas sobre el mismo vuelo.

LO UNICO QUE AGREGA son puntualidad y cancelaciones, que `msrtic` no mira porque
al mapa no le importan: salen de comparar `programada_epoch` con `real_epoch` en
las partidas crudas de `quien_cargo.tablero()`.

POR QUE LOS m3 SON MAS PRECISOS ACA QUE EN EL MAPA. El mapa historico estima el
avion de cada ruta-mes con `seleccionar_avion()`. Cuando la partida trae matricula
y el registro la tiene, el tipo es el REAL: no se estima el avion, se lo sabe.
Cada fila dice cuantos de sus vuelos fueron medidos asi y cuantos estimados, y la
pantalla lo muestra -- un m3 medido y uno estimado no valen lo mismo.
"""
import time

import msrtic
import quien_cargo
import avion_model

# Un vuelo es PUNTUAL si sale dentro de estos minutos de su hora programada. 15 es
# el umbral que usa la industria (DOT/OTP15), no una eleccion de este repo.
PUNTUAL_MIN = 15


def _min_atraso(f):
    """Minutos de atraso de una partida, o None si no se puede saber."""
    prog, real = f.get("programada_epoch"), f.get("real_epoch")
    if not prog or not real:
        return None
    return (real - prog) / 60.0


def _puntualidad(filas):
    """{aerolinea_id: {...}} con atraso, puntualidad y cancelaciones.

    SALIR ANTES NO COMPENSA SALIR TARDE. El atraso promedio cuenta las salidas
    adelantadas como 0 y no como un negativo: si no, una compania que sale 20
    minutos antes y 20 despues promediaria "en horario" y no lo esta.

    ponytail: promedio simple, alcanza para comparar companias. Si alguna vez
    hace falta la distribucion (mediana, p90), sale de la misma lista de atrasos.
    """
    out = {}
    for f in filas:
        k = (f.get("aerolinea_id") or "").strip().upper()
        d = out.setdefault(k, {"atrasos": [], "cancelados": 0, "vuelos": 0, "salieron": 0})
        d["vuelos"] += 1
        sit = f.get("situacion")
        if sit == "no_salio":
            d["cancelados"] += 1
        elif sit == "despego":
            d["salieron"] += 1
        m = _min_atraso(f)
        if m is not None:
            d["atrasos"].append(m)
    for d in out.values():
        a = d["atrasos"]
        d["con_hora"] = len(a)
        d["atraso_prom_min"] = round(sum(max(m, 0) for m in a) / len(a), 1) if a else None
        d["puntualidad"] = (round(100.0 * sum(1 for m in a if m <= PUNTUAL_MIN) / len(a), 1)
                            if a else None)

        # EL DENOMINADOR SON LAS PARTIDAS YA RESUELTAS: salio o no salio. Las que
        # todavia no despegaron y las que no tienen dato NO entran -- incluirlas haria
        # que la tasa de cancelacion baje sola a lo largo del dia a medida que se llena
        # el tablero, y que a la manana toda aerolinea parezca perfecta.
        resueltas = d["salieron"] + d["cancelados"]
        d["resueltas"] = resueltas
        d["cancelados_pct"] = (round(100.0 * d["cancelados"] / resueltas, 1)
                               if resueltas else None)
        # Completion factor: el nombre que usa la industria para "de lo programado,
        # cuanto se llego a operar".
        completion = (100.0 - d["cancelados_pct"]) if d["cancelados_pct"] is not None else None

        # INDICE DE SERVICIO = completion x puntualidad. Es la probabilidad de que un
        # vuelo salga Y salga a horario, en un solo numero.
        #
        # Multiplicar y no promediar con pesos: un promedio ponderado necesita elegir
        # cuanto pesa cada mitad, y esa eleccion no sale de ningun lado. El producto no
        # tiene parametros y ademas se lee literal -- "de cada 100 partidas resueltas,
        # tantas salieron dentro de los 15 minutos" -- porque son dos condiciones que
        # se tienen que cumplir las dos.
        #
        # OJO CON EL SESGO: la puntualidad se mide sobre las partidas que TIENEN hora
        # real medida, que son menos que las resueltas. `con_hora` y `resueltas` viajan
        # al lado para que se pueda ver sobre cuanto se calculo cada mitad; con pocas
        # horas medidas el indice dice mas de la cobertura del feed que de la aerolinea.
        d["indice_servicio"] = (round(completion * d["puntualidad"] / 100.0, 1)
                                if (completion is not None and d["puntualidad"] is not None)
                                else None)
        del d["atrasos"]
    return out


def _asientos(fila):
    """Asientos promedio de la fila, PONDERADOS por cuantas veces volo cada tipo.

    Antes se usaba el tipo mas frecuente para toda la fila y eso daba ocupaciones
    imposibles: Andes marcaba 108,5% porque sus vuelos mezclan tipos y el denominador
    salia del avion mas chico. Un porcentaje mayor a 100 no es un redondeo, es un
    denominador equivocado.

    Los tipos sin asientos en la flota no entran en el promedio en vez de contar como
    cero: un avion que no conocemos no achica los asientos de los que si.
    """
    flota = avion_model.get_flota()
    cuentas = fila.get("aviones_n") or {c: 1 for c in (fila.get("aviones") or [])}
    total_asientos, total_vuelos = 0, 0
    for codigo, n in cuentas.items():
        ficha = flota.get(codigo)
        if ficha and ficha.get("asientos"):
            total_asientos += int(ficha["asientos"]) * n
            total_vuelos += n
    return (total_asientos / total_vuelos) if total_vuelos else None


def _nuevo():
    return {"vuelos": 0, "m3": 0.0, "pax": 0, "vuelos_con_pax": 0, "asientos": 0,
            "medidos": 0, "estimados": 0, "aviones": {}, "m3_por_proveedor": {}}


def _acumular(destino, f):
    """Suma una fila de msrtic al acumulador de un grupo (aerolinea o ruta)."""
    destino["vuelos"] += f["vuelos"]
    destino["m3"] += f["m3"]
    destino["medidos"] += f["aviones_medidos"]
    destino["estimados"] += f["aviones_estimados"]
    if f.get("pax"):
        destino["pax"] += f["pax"]
        destino["vuelos_con_pax"] += f["vuelos_con_pax_pos"]
        asientos = _asientos(f)
        if asientos:
            destino["asientos"] += asientos * f["vuelos_con_pax_pos"]
    for c in f.get("aviones") or []:
        destino["aviones"][c] = destino["aviones"].get(c, 0) + 1
    destino["m3_por_proveedor"][f["proveedor"]] = (
        destino["m3_por_proveedor"].get(f["proveedor"], 0.0) + f["m3"])


def _cerrar(d):
    """Convierte un acumulador en los numeros que muestra la pantalla."""
    m3 = d["m3"]
    ypf = d["m3_por_proveedor"].get("YPF", 0.0)
    sin_declarar = d["m3_por_proveedor"].get("sin_declarar", 0.0)
    return {
        "vuelos": d["vuelos"],
        "m3": round(m3, 1),
        "pax": d["pax"] or None,
        "vuelos_con_pax": d["vuelos_con_pax"],
        # La ocupacion solo se calcula sobre los vuelos que informaron pax > 0 Y cuyo
        # tipo de avion conocemos: sin asientos no hay denominador. Si no hay ninguno,
        # es None y la pantalla dice "sin dato" en vez de dibujar un 0%.
        #
        # PUEDE PASAR DE 100 Y NO SE CAPA. Dos motivos reales, los dos del dato y no de
        # la cuenta: (1) `asientos` de avion_model es UNA configuracion, no la del avion
        # que volo -- un B738 figura con 170 y se vende hasta con 189, y Andes informo
        # 185 pax en uno; (2) el pax viene de unos pocos vuelos de la fila y el
        # denominador promedia los aviones de TODOS, porque el feed no dice que avion
        # hizo cual. Capar a 100 esconderia las dos cosas y dejaria la ocupacion
        # pareciendo exacta. Un valor arriba de 100 se lee como "el avion real tenia mas
        # asientos que el de referencia", y la pantalla lo dice.
        "ocupacion": (round(100.0 * d["pax"] / d["asientos"], 1)
                      if d["asientos"] and d["pax"] else None),
        # El share va como RANGO, igual que en el banner del mapa: piso son los m3 de
        # YPF confirmados, techo suma los que nadie reclamo. Un numero solo seria
        # elegir una de las dos puntas sin decirlo.
        "ms_ypf_piso": round(100.0 * ypf / m3, 1) if m3 else None,
        "ms_ypf_techo": round(100.0 * (ypf + sin_declarar) / m3, 1) if m3 else None,
        "m3_ypf": round(ypf, 1),
        "aviones_medidos": d["medidos"],
        "aviones_estimados": d["estimados"],
        "aviones": sorted(d["aviones"], key=lambda c: -d["aviones"][c]),
        "m3_por_proveedor": {k: round(v, 1) for k, v in sorted(d["m3_por_proveedor"].items())},
    }


def proveedores_conocidos():
    """Los nombres de petrolera que la planilla ya usa, con su case tal cual.

    SALEN DE LA PLANILLA Y NO DE LOS DATOS DEL FILTRO. Derivarlos de las filas que se
    estan mostrando parece equivalente y no lo es: con una aerolinea filtrada, el
    recorte solo tiene las petroleras de SUS rutas -- Lufthansa vuela solo rutas de YPF
    -- y el selector se quedaba sin la opcion que se queria elegir. Peor todavia,
    elegirla dejaba el <select> en vacio y eso EN ESTA PANTALLA significa "borrar la
    declaracion": el usuario pedia Raizen y borraba.
    """
    try:
        import json
        with open(msrtic.tabla_proveedores(), encoding='utf-8-sig') as f:
            d = json.load(f)
    except (OSError, ValueError):
        return ['YPF']
    nombres = set((d.get('rutas') or {}).values())
    for v in (d.get('por_aerolinea') or {}).values():
        nombres.update(v.values())
    nombres.add('YPF')
    return sorted(n for n in nombres if n)


def _codigos(aerolinea):
    """set de codigos en mayuscula desde un string, un "AR,WJ" o una lista. Vacio = todas."""
    if not aerolinea:
        return set()
    if isinstance(aerolinea, str):
        aerolinea = aerolinea.split(",")
    return {str(a).strip().upper() for a in aerolinea if str(a).strip()}


def analisis(dia=None, horas=24.0, aerolinea=None, tipo=None, desde=None, hasta=None):
    """Todo lo que muestra la pantalla, en una sola pasada.

    `aerolinea` es un codigo (AR) o varios (["AR", "WJ"] o "AR,WJ") y filtra TODO
    menos la lista de companias, que se arma siempre completa: si el filtro la
    recortara, el selector se quedaria sin las opciones que no estan en el recorte
    y no habria como volver ni como agregar otra.

    `tipo` es 'cabotaje' | 'internacional' | None (las dos).

    SIN `dia` NI `horas` SE MUESTRA TODO. `horas=None` es la salida explicita al
    historico completo: con un `horas` puesto y `dia=None`, la pantalla decia "todo
    el historico" y mostraba las ultimas 24 h, que es peor que no tener la opcion.
    """
    fs, est = msrtic.filas(horas, dia=dia, desde=desde, hasta=hasta)

    # La lista de companias sale de TODAS las filas, antes de filtrar (ver docstring).
    todas = {}
    for f in fs:
        todas.setdefault(f["aerolinea_id"] or f["aerolinea"],
                         {"id": f["aerolinea_id"], "nombre": f["aerolinea"]})

    if tipo in ("cabotaje", "internacional"):
        fs = [f for f in fs if f["tipo"] == tipo]
    elegidas = _codigos(aerolinea)
    if elegidas:
        fs = [f for f in fs if (f["aerolinea_id"] or "").upper() in elegidas]

    total, por_aero, por_ruta = _nuevo(), {}, {}
    for f in fs:
        _acumular(total, f)
        _acumular(por_aero.setdefault(f["aerolinea_id"] or f["aerolinea"], _nuevo()), f)
        r = por_ruta.setdefault((f["tipo"], f["origin"], f["dest"]), _nuevo())
        _acumular(r, f)
        # El proveedor y la distancia son propiedad de la RUTA, no del grupo: se
        # copian, no se acumulan.
        r["proveedor"], r["motivo"] = f["proveedor"], f["motivo"]
        r["distancia_km"] = f.get("distancia_km")
        r["cod_o"], r["cod_d"] = f.get("cod_o"), f.get("cod_d")

    # Puntualidad: partidas crudas, no las filas agregadas de msrtic.
    #
    # `tablero()` entiende dia y ventana de horas, pero NO rango: con un rango pedido se
    # le pide todo y se recorta aca con el mismo criterio que usa msrtic -- dia LOCAL de
    # la partida, inclusivo en las dos puntas. Filtrar distinto que msrtic haria que la
    # puntualidad hablara de un conjunto de vuelos y los m3 de otro, en la misma fila.
    if desde or hasta:
        tab = quien_cargo.tablero(msrtic.base_oficial(), horas=None)
        d0, d1 = (desde or "0000-00-00"), (hasta or "9999-99-99")
        if d0 > d1:
            d0, d1 = d1, d0
        vuelos = [v for v in (tab["vuelos"] if tab else [])
                  if d0 <= (quien_cargo.dia_de(v) or "") <= d1]
    else:
        tab = quien_cargo.tablero(msrtic.base_oficial(), horas=horas, dia=dia)
        vuelos = tab["vuelos"] if tab else []
    punt = _puntualidad(vuelos)

    aerolineas = []
    for k, d in por_aero.items():
        fila = _cerrar(d)
        fila["id"] = todas.get(k, {}).get("id") or k
        fila["nombre"] = todas.get(k, {}).get("nombre") or k
        fila.update({x: y for x, y in punt.get((fila["id"] or "").upper(), {}).items()
                     if x != "vuelos"})
        aerolineas.append(fila)
    aerolineas.sort(key=lambda x: -x["m3"])

    rutas = []
    for (t, o, d), v in por_ruta.items():
        fila = _cerrar(v)
        fila.update(tipo=t, origin=o, dest=d,
                    # Los IATA viajan para que la pantalla pueda editar la planilla,
                    # que se indexa por codigo y no por nombre de aeropuerto.
                    cod_o=v.get("cod_o"), cod_d=v.get("cod_d"),
                    proveedor=v["proveedor"],
                    motivo=v["motivo"], distancia_km=v.get("distancia_km"),
                    # "Cubierta" es que la planilla diga que carga YPF. Un
                    # 'sin_declarar' NO es una ruta que perdimos: es una que nadie
                    # declaro, y mezclarlas inventaria una perdida.
                    cubierta=v["proveedor"] == "YPF")
        rutas.append(fila)
    rutas.sort(key=lambda x: -x["m3"])

    salida = _cerrar(total)
    salida.update(
        aerolineas=aerolineas,
        rutas=rutas,
        companias=sorted(todas.values(), key=lambda x: x["nombre"] or ""),
        proveedores=proveedores_conocidos(),
        filtro={"dia": dia, "horas": horas, "aerolinea": sorted(elegidas) or None,
                "tipo": tipo, "desde": desde, "hasta": hasta},
        estado=est,
        generado=time.time(),
    )
    return salida


def _self_check():
    """Chequeo minimo de las cuentas, sin tocar la base: filas armadas a mano."""
    base = {"tipo": "cabotaje", "origin": "A", "dest": "B", "aerolinea": "Aero",
            "aerolinea_id": "AR", "proveedor": "YPF", "motivo": "planilla",
            "vuelos": 2, "pax": 300, "vuelos_con_pax": 2, "vuelos_con_pax_pos": 2,
            "m3": 10.0, "aviones_medidos": 2, "aviones_estimados": 0,
            "aviones": ["ZZZZ"], "distancia_km": 100.0}

    d = _nuevo()
    _acumular(d, base)
    r = _cerrar(d)
    assert r["vuelos"] == 2 and r["m3"] == 10.0, r
    assert r["ms_ypf_piso"] == 100.0 and r["ms_ypf_techo"] == 100.0, r
    # Sin asientos conocidos no se inventa un porcentaje de ocupacion.
    assert r["ocupacion"] is None, r

    # Un competidor baja el piso Y el techo. 'sin_declarar' baja solo el piso: es
    # lo unico que separa las dos puntas del rango.
    d2 = _nuevo()
    _acumular(d2, base)
    _acumular(d2, dict(base, proveedor="Axion"))
    r2 = _cerrar(d2)
    assert r2["ms_ypf_piso"] == 50.0 and r2["ms_ypf_techo"] == 50.0, r2

    d3 = _nuevo()
    _acumular(d3, base)
    _acumular(d3, dict(base, proveedor="sin_declarar"))
    r3 = _cerrar(d3)
    assert r3["ms_ypf_piso"] == 50.0 and r3["ms_ypf_techo"] == 100.0, r3

    # Asientos ponderados: con dos tipos, el promedio pesa por cuantas veces volo cada
    # uno. Es lo que evita ocupaciones mayores a 100.
    flota = avion_model.get_flota()
    par = [c for c in flota if flota[c].get("asientos")][:2]
    if len(par) == 2:
        a, b = par
        sa, sb = flota[a]["asientos"], flota[b]["asientos"]
        uno = _asientos({"aviones_n": {a: 3, b: 1}})
        assert abs(uno - (sa * 3 + sb) / 4.0) < 1e-6, uno
        # Un tipo desconocido no entra al promedio en vez de contar como cero asientos.
        dos = _asientos({"aviones_n": {a: 1, "ZZZZ": 5}})
        assert abs(dos - sa) < 1e-6, dos

    # Varias aerolineas a la vez, en cualquiera de las tres formas de pasarlas.
    assert _codigos(None) == set()
    assert _codigos("ar") == {"AR"}
    assert _codigos("AR,WJ") == {"AR", "WJ"}
    assert _codigos([" ar ", "wj", ""]) == {"AR", "WJ"}

    # Cancelaciones e indice de servicio.
    q = _puntualidad([
        # 2 salieron (una puntual, una tarde), 1 cancelado, 1 todavia programado.
        {"aerolinea_id": "AR", "programada_epoch": 1000, "real_epoch": 1000 + 5 * 60,
         "situacion": "despego"},
        {"aerolinea_id": "AR", "programada_epoch": 1000, "real_epoch": 1000 + 60 * 60,
         "situacion": "despego"},
        {"aerolinea_id": "AR", "situacion": "no_salio"},
        {"aerolinea_id": "AR", "situacion": "programada"},
    ])["AR"]
    # El denominador son las 3 resueltas, NO las 4 filas: la programada no se cuenta.
    assert q["resueltas"] == 3, q
    assert q["cancelados_pct"] == 33.3, q
    assert q["puntualidad"] == 50.0, q
    # completion 66.7% x puntualidad 50% = 33.4%
    assert q["indice_servicio"] == 33.4, q

    # Puntualidad: un vuelo adelantado no compensa a uno atrasado.
    p = _puntualidad([
        {"aerolinea_id": "AR", "programada_epoch": 1000, "real_epoch": 1000 + 20 * 60,
         "situacion": "despego"},
        {"aerolinea_id": "AR", "programada_epoch": 1000, "real_epoch": 1000 - 20 * 60,
         "situacion": "despego"},
        {"aerolinea_id": "AR", "situacion": "no_salio"},
    ])["AR"]
    assert p["atraso_prom_min"] == 10.0, p
    assert p["puntualidad"] == 50.0, p
    assert p["cancelados"] == 1 and p["con_hora"] == 2, p
    print("analisis_quien_cargo: OK")


if __name__ == "__main__":
    _self_check()
