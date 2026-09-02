"""Feed de noticias de aviación comercial para /noticias.

Intenta leer el JSON de la automatización NOTICIAS AVIACION (env BRIEFING_API).
Si no hay URL o falla la red, usa el briefing sembrado de la última corrida.
"""
import json
import os
import urllib.error
import urllib.request
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo
import time

SEGMENT_LABEL = {
    "combustible": "Combustible",
    "mercado": "Mercado",
    "rutas": "Rutas",
    "flota": "Flota",
    "regulacion": "Regulación",
}

FUEL_LABEL = {
    "jet": "JET A-1",
    "avgas": "AVGAS",
    "saf": "SAF",
    "ninguno": "—",
}

SEGMENT_ORDER = ("combustible", "mercado", "rutas", "flota", "regulacion")

SEED_BRIEFING = {
    "title": "Crisis Jet Fuel Impacta Aerolíneas Argentinas",
    "summary": (
        "Resumen de la automatización NOTICIAS AVIACION (corrida del 21 ago 2026). "
        "El foco es aviación comercial en Argentina y el negocio de JET A-1 / AVGAS — "
        "sin promociones ni descuentos. En Aviacionline, la nota de mayor peso para el "
        "combustible es el recargo por Jet Fuel de Aerolíneas Argentinas (7.500 ARS en "
        "cabotaje y USD 10–50 en internacional), heredado de la crisis de precios del Golfo. "
        "Sir Chandler no publicó cobertura reciente de venta o abastecimiento de JET/AVGAS; "
        "su archivo de marzo documenta los mismos copagos. En paralelo, las Alertas de Google "
        "marcan un mercado local en reacomodo: colapso operativo de Flybondi, Andes ganando "
        "share, Joy Airline abriendo Jujuy–Córdoba–Buenos Aires, ANAC abaratando el ingreso "
        "de extranjeras, y Brasil adelantándose en SAF mientras Argentina queda rezagada. "
        "Para YPF Aviación el combo es claro: menos vuelos de low-cost, más recargos por "
        "Jet Fuel, y nuevas rutas regionales que mueven demanda de JET A-1 en el interior."
    ),
    "generatedAt": "2026-08-21T15:53:15.000Z",
    "source": "automation",
}

SEED_NOTICIAS = [
    {
        "title": "Crisis en Medio Oriente: Aerolíneas Argentinas aplica recargos por combustible",
        "summary": "Recargo de 7.500 pesos por tramo en cabotaje y entre 10 y 50 dólares en internacional, regional y largo radio. El Jet A-1 representa 30–40% del costo operativo; el alza del Brent y del crack spread presiona márgenes. Es la noticia de combustible más directa para el negocio de aviación comercial en Argentina.",
        "source": "Aviacionline",
        "sourceUrl": "https://www.aviacionline.com/espanol/aviacion-comercial/latinoamerica-y-el-caribe/argentina/crisis-en-medio-oriente-aerolineas-argentinas-aplica-recargos-por-combustible-en-sus-pasajes_a69b1c39c2a95660dd254ba62",
        "publishedAt": "2026-03-11",
        "airline": "Aerolíneas Argentinas",
        "segment": "combustible",
        "fuelTag": "jet",
        "relevance": "alta",
    },
    {
        "title": "La era de los copagos llegó a las aerolíneas por el aumento del combustible",
        "summary": "Sir Chandler confirma el mismo esquema de recargos de Aerolíneas: $7.500 en cabotaje y USD 10–50 en internacionales. No hay notas recientes sobre venta o abastecimiento de JET/AVGAS; el archivo de marzo sigue siendo la referencia del sitio sobre el lado combustible.",
        "source": "Sir Chandler",
        "sourceUrl": "https://www.sirchandler.com.ar/2026/03/la-era-de-los-copagos-llego-a-las-aerolineas-por-el-aumento-del-combustible/",
        "publishedAt": "2026-03-11",
        "airline": "Aerolíneas Argentinas",
        "segment": "combustible",
        "fuelTag": "jet",
        "relevance": "alta",
    },
    {
        "title": "Radiografía del colapso de Flybondi: flota paralizada y deudas millonarias",
        "summary": "La low-cost opera con flota mínima, sitio caído y deudas con proveedores. Menos frecuencias de Boeing 737 implica menos offtake de JET A-1 en Ezeiza, Aeroparque y destinos de cabotaje. Impacto directo sobre volúmenes de combustible, no sobre tarifas promocionales.",
        "source": "Aviación al Día",
        "sourceUrl": "https://aviacionaldia.com/2026/08/radiografia-del-colapso-de-flybondi-paralisis-de-flota-sitio-web-fuera-de-servicio-y-deudas-millonarias.html",
        "publishedAt": "2026-08-21",
        "airline": "Flybondi",
        "segment": "mercado",
        "fuelTag": "jet",
        "relevance": "alta",
    },
    {
        "title": "Con Flybondi agonizando, Andes la superó en market share por primera vez en 8 años",
        "summary": "Andes desplaza a Flybondi en participación de cabotaje. El tráfico se redistribuye hacia un operador más chico, con otra red y otra curva de consumo de JET A-1 — menos concentración en bases de bajo costo y más peso relativo en rutas regionales.",
        "source": "Aviacionline",
        "sourceUrl": "https://www.aviacionline.com/espanol/aviacion-comercial/latinoamerica-y-el-caribe/argentina/con-flybondi-agonizando-andes-la-supero-en-market-share-por-primera-vez-en-8-anos_a6a7f444ca2a97ba00dfc4afd",
        "publishedAt": "2026-08-15",
        "airline": "Andes",
        "segment": "mercado",
        "fuelTag": "jet",
        "relevance": "alta",
    },
    {
        "title": "La Justicia ordenó embargos contra Flybondi por deudas con ARCA",
        "summary": "Embargos de cuentas en medio de la crisis operativa. Riesgo de quiebra o recorte adicional de red: cada avión en tierra es un punto de abastecimiento de JET A-1 que deja de mover volumen.",
        "source": "Aviacionline",
        "sourceUrl": "https://www.aviacionline.com/espanol/aviacion-comercial/latinoamerica-y-el-caribe/argentina/la-justicia-ordeno-embargos-contra-flybondi-por-deudas-con-arca_a6a7dd5696017ed071ad979ee",
        "publishedAt": "2026-08-14",
        "airline": "Flybondi",
        "segment": "mercado",
        "fuelTag": "ninguno",
        "relevance": "media",
    },
    {
        "title": "Joy Airline empezaría a volar desde Jujuy a Córdoba y Buenos Aires en septiembre",
        "summary": "Nueva regional con CRJ-200: JUJ–COR y JUJ–BUE. Añade demanda de JET A-1 en Gobernador Guzmán y en las bases de Córdoba y Buenos Aires. Es entrada neta de un cliente de combustible, no una promo de pasajes.",
        "source": "Aviacionline",
        "sourceUrl": "https://www.aviacionline.com/espanol/aviacion-comercial/latinoamerica-y-el-caribe/argentina/joy-airline-empezaria-a-volar-desde-jujuy-a-cordoba-y-buenos-aires-en-septiembre_a6a7db1e86017ed071ad14469",
        "publishedAt": "2026-08-14",
        "airline": "Joy Airline",
        "segment": "rutas",
        "fuelTag": "jet",
        "relevance": "alta",
    },
    {
        "title": "ANAC simplifica los trámites para el ingreso de nuevas aerolíneas internacionales",
        "summary": "Baja de barreras de entrada para extranjeras. Más operadores en Ezeiza y Aeroparque implica más slots de abastecimiento de JET A-1 y competencia por el contrato de combustible en rampa.",
        "source": "Aviación al Día",
        "sourceUrl": "https://aviacionaldia.com/2026/08/anac-simplifica-los-tramites-para-el-ingreso-de-nuevas-aerolineas-internacionales-a-argentina.html",
        "publishedAt": "2026-08-11",
        "airline": None,
        "segment": "regulacion",
        "fuelTag": "jet",
        "relevance": "alta",
    },
    {
        "title": "LATAM suma vuelos entre São Paulo y Mendoza",
        "summary": "Nueva frecuencia GRU–MDZ en A320. Suma offtake de JET A-1 en El Plumerillo y refuerza el corredor Argentina–Brasil, el de mayor crecimiento de tráfico regional en el año.",
        "source": "Aviation Club Center",
        "sourceUrl": "https://aviationclubcenter.com/es/2026/08/10/latam-suma-vuelos-entre-sao-paulo-y-mendoza/",
        "publishedAt": "2026-08-10",
        "airline": "LATAM",
        "segment": "rutas",
        "fuelTag": "jet",
        "relevance": "media",
    },
    {
        "title": "Aviación comercial argentina recibe autorización para cruzar el espacio aéreo venezolano",
        "summary": "Un A330-243 argentino obtuvo permiso para atravesar Venezuela. Reabre geometría de rutas al Caribe y al norte, con impacto en consumo de JET A-1 en tramos de largo radio que antes se desviaban.",
        "source": "Informe Aéreo",
        "sourceUrl": "https://informeaereo.com/venezuela/aviacion-comercial-argentina-recibe-autorizacion-para-cruzar-el-espacio-aereo-venezolano/",
        "publishedAt": "2026-08-21",
        "airline": "Aerolíneas Argentinas",
        "segment": "rutas",
        "fuelTag": "jet",
        "relevance": "media",
    },
    {
        "title": "Argentina busca frenar en Estados Unidos el pago de 390 millones de dólares por Aerolíneas",
        "summary": "El Estado intenta evitar embargos en EE. UU. tras el laudo del CIADI por Titan Consortium. Riesgo reputacional y de flota para el operador de mayor consumo de JET A-1 del país.",
        "source": "Aviacionline",
        "sourceUrl": "https://www.aviacionline.com/espanol/aviacion-comercial/latinoamerica-y-el-caribe/argentina/argentina-busca-frenar-en-estados-unidos-el-pago-de-390-millones-de-dolares-por-aerolineas-argentinas_a6a8721f7b6c2ebc45b94697e",
        "publishedAt": "2026-08-21",
        "airline": "Aerolíneas Argentinas",
        "segment": "mercado",
        "fuelTag": "ninguno",
        "relevance": "media",
    },
    {
        "title": "Brasil acelera los biocombustibles para aviación y Argentina queda rezagada",
        "summary": "Lula reglamentó el programa nacional de SAF con metas obligatorias desde 2027. Argentina no produce SAF a escala; la brecha regional se agranda y presiona a YPF Aviación sobre el calendario de combustible sostenible.",
        "source": "Agritotal",
        "sourceUrl": "https://www.agritotal.com/nota/brasil-acelera-los-biocombustibles-para-aviacion-y-barcos-y-argentina-queda-rezagada-3410/",
        "publishedAt": "2026-08-14",
        "airline": None,
        "segment": "combustible",
        "fuelTag": "saf",
        "relevance": "alta",
    },
    {
        "title": "Autorizan vuelos Buenos Aires / Ezeiza – Medellín de JetSMART",
        "summary": "Nueva operación internacional de la low-cost. Suma un cliente de JET A-1 en Ezeiza sobre una ruta andina de alto factor de ocupación, sin tratarse de una campaña de precios.",
        "source": "Aviacionline",
        "sourceUrl": "https://www.aviacionline.com/espanol/aviacion-comercial/latinoamerica-y-el-caribe/argentina",
        "publishedAt": "2026-08-05",
        "airline": "JetSMART",
        "segment": "rutas",
        "fuelTag": "jet",
        "relevance": "media",
    },
]


def _normalize_item(raw, idx):
    fuel = (raw.get("fuelTag") or "ninguno").lower()
    if fuel not in FUEL_LABEL:
        fuel = "ninguno"
    segment = (raw.get("segment") or "mercado").lower()
    if segment not in SEGMENT_LABEL:
        segment = "mercado"
    published = raw.get("publishedAt") or ""
    if published and "T" in published:
        published = published[:10]
    return {
        "id": raw.get("id") or idx,
        "title": raw.get("title") or "",
        "summary": raw.get("summary") or "",
        "source": raw.get("source") or "",
        "sourceUrl": raw.get("sourceUrl") or raw.get("source_url") or "",
        "publishedAt": published,
        "airline": raw.get("airline") or "—",
        "segment": segment,
        "segment_label": SEGMENT_LABEL[segment],
        "fuelTag": fuel,
        "fuel_label": FUEL_LABEL[fuel],
        "relevance": raw.get("relevance") or "media",
    }


def _from_payload(payload):
    if not isinstance(payload, dict):
        payload = {}
    briefing = payload.get("briefing")
    if not isinstance(briefing, dict) or not briefing:
        briefing = {
            "title": payload.get("title") or "",
            "summary": payload.get("summary") or "",
            "generatedAt": payload.get("generatedAt") or "",
            "source": payload.get("source") or "automation",
        }
    noticias = [_normalize_item(n, i) for i, n in enumerate(payload.get("noticias") or [], start=1)]
    counts = payload.get("counts") or {
        "total": len(noticias),
        "combustible": sum(1 for n in noticias if n["segment"] == "combustible"),
        "jet": sum(1 for n in noticias if n["fuelTag"] == "jet"),
        "avgas": sum(1 for n in noticias if n["fuelTag"] == "avgas"),
        "saf": sum(1 for n in noticias if n["fuelTag"] == "saf"),
    }
    return briefing, noticias, counts


def _from_seed():
    payload = {
        "briefing": SEED_BRIEFING,
        "noticias": SEED_NOTICIAS,
        "counts": {
            "total": len(SEED_NOTICIAS),
            "combustible": sum(1 for n in SEED_NOTICIAS if n["segment"] == "combustible"),
            "jet": sum(1 for n in SEED_NOTICIAS if n["fuelTag"] == "jet"),
            "avgas": sum(1 for n in SEED_NOTICIAS if n["fuelTag"] == "avgas"),
            "saf": sum(1 for n in SEED_NOTICIAS if n["fuelTag"] == "saf"),
        },
    }
    return _from_payload(payload)


DEFAULT_FEED_URL = (
    "https://raw.githubusercontent.com/noguerafran-alt/noticias-aviacion-feed/main/noticias-feed.json"
)
_CACHE = {"at": 0.0, "payload": None}
_CACHE_TTL = 600


def _fetch_remote():
    url = (os.environ.get("BRIEFING_API") or DEFAULT_FEED_URL).strip()
    if not url:
        return None
    now = time.time()
    if _CACHE["payload"] is not None and (now - _CACHE["at"]) < _CACHE_TTL:
        return _CACHE["payload"]
    req = urllib.request.Request(
        url,
        headers={"Accept": "application/json", "User-Agent": "mapa-negocio"},
    )
    with urllib.request.urlopen(req, timeout=4) as resp:
        payload = json.loads(resp.read().decode("utf-8"))
    if not isinstance(payload, dict):
        return None
    _CACHE["payload"] = payload
    _CACHE["at"] = now
    return payload


def _parse_day(value):
    if not value:
        return None
    try:
        return date.fromisoformat(str(value)[:10])
    except ValueError:
        return None


_MESES = ("ene", "feb", "mar", "abr", "may", "jun", "jul", "ago", "sep", "oct", "nov", "dic")


def _today_ar():
    return datetime.now(ZoneInfo("America/Buenos_Aires")).date()


def _week_start(day):
    return day - timedelta(days=day.weekday())


def _fmt_span(start, end):
    if start.month == end.month:
        return f"{start.day}–{end.day} {_MESES[start.month - 1]}"
    return f"{start.day} {_MESES[start.month - 1]} – {end.day} {_MESES[end.month - 1]}"


def _fecha_label(value, today):
    day = _parse_day(value)
    if not day:
        return "—"
    mes = _MESES[day.month - 1]
    if day.year == today.year:
        return f"{day.day} {mes}"
    return f"{day.day} {mes} {str(day.year)[2:]}"


PERIODO_PRESETS = [
    ("7d", "Últimos 7 días"),
    ("prev", "Semana anterior"),
    ("28d", "Últimas 4 semanas"),
    ("todas", "Todo el histórico"),
]


def _periodo_options(noticias, today):
    return list(PERIODO_PRESETS)


def _in_periodo(day, periodo, today):
    if not day:
        return False
    if periodo == "todas":
        return True
    if periodo == "prev":
        return today - timedelta(days=14) <= day < today - timedelta(days=7)
    if periodo == "28d":
        return day >= today - timedelta(days=28)
    if periodo.startswith("w:"):
        try:
            start = date.fromisoformat(periodo[2:])
        except ValueError:
            start = None
        if start:
            return start <= day <= start + timedelta(days=6)
    return day >= today - timedelta(days=7)


def load_noticias_feed(segment="todas", periodo="7d"):
    error = None
    try:
        remote = _fetch_remote()
        if remote:
            briefing, noticias, counts = _from_payload(remote)
        else:
            briefing, noticias, counts = _from_seed()
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, ValueError, json.JSONDecodeError) as exc:
        briefing, noticias, counts = _from_seed()
        error = f"No se pudo leer el API remoto; se muestra el último briefing local. ({exc.__class__.__name__})"

    today = _today_ar()
    dias = sorted({
        day
        for n in noticias
        if (day := _parse_day(n.get("publishedAt")))
    })
    weeks = sorted({_week_start(d) for d in dias})
    periodos = _periodo_options(noticias, today)
    valid = {key for key, _ in periodos}
    if periodo.startswith("w:"):
        try:
            picked = date.fromisoformat(periodo[2:12])
            start = _week_start(picked)
            periodo = f"w:{start.isoformat()}"
        except ValueError:
            periodo = "7d"
    elif periodo not in valid:
        periodo = "7d"

    latest = dias[-1] if dias else today
    has_calendar_7d = any(d >= today - timedelta(days=7) for d in dias)
    # Si "últimos 7 días" está vacío (la auto corre los miércoles y el recorte
    # queda viejo), mostramos los 7 días que cierran en la nota más reciente.
    fallback_7d = periodo == "7d" and not has_calendar_7d

    filtradas = []
    for n in noticias:
        day = _parse_day(n.get("publishedAt"))
        if fallback_7d:
            ok = bool(day and day >= latest - timedelta(days=7))
        else:
            ok = _in_periodo(day, periodo, today)
        if ok:
            n["fecha_label"] = _fecha_label(n.get("publishedAt"), today)
            filtradas.append(n)
    filtradas.sort(key=lambda n: n.get("publishedAt") or "", reverse=True)
    noticias = filtradas
    counts = {
        "total": len(noticias),
        "combustible": sum(1 for n in noticias if n["segment"] == "combustible"),
        "jet": sum(1 for n in noticias if n["fuelTag"] == "jet"),
        "avgas": sum(1 for n in noticias if n["fuelTag"] == "avgas"),
        "saf": sum(1 for n in noticias if n["fuelTag"] == "saf"),
    }

    if segment and segment != "todas":
        noticias = [n for n in noticias if n["segment"] == segment]

    grupos = []
    for key in SEGMENT_ORDER:
        items = [n for n in noticias if n["segment"] == key]
        if items:
            grupos.append({"id": key, "label": SEGMENT_LABEL[key], "notas": items})

    if periodo.startswith("w:"):
        start = date.fromisoformat(periodo[2:])
        periodo_label = f"Semana {_fmt_span(start, start + timedelta(days=6))}"
    elif fallback_7d:
        periodo_label = f"Último recorte ({_fmt_span(latest - timedelta(days=7), latest)})"
    else:
        periodo_label = next((label for key, label in periodos if key == periodo), "Últimos 7 días")
    generated = (briefing.get("generatedAt") or "")[:10]
    cal_view = today
    if periodo.startswith("w:"):
        cal_view = date.fromisoformat(periodo[2:])
    elif dias:
        cal_view = dias[-1]
    return {
        "briefing": briefing,
        "grupos": grupos,
        "counts": counts,
        "segment_sel": segment or "todas",
        "segmentos": [("todas", "Todas")] + [(k, SEGMENT_LABEL[k]) for k in SEGMENT_ORDER],
        "periodo_sel": periodo,
        "periodos": periodos,
        "periodo_label": periodo_label,
        "generated_day": generated,
        "error": error,
        "cal": {
            "weeks": [w.isoformat() for w in weeks],
            "days": [d.isoformat() for d in dias],
            "selected": periodo,
            "min": (dias[0].isoformat() if dias else today.isoformat()),
            "max": today.isoformat(),
            "view": f"{cal_view.year}-{cal_view.month:02d}",
        },
    }
