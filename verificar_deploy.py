"""Verifica que el build desplegado sea el nuevo y no el anterior.

Chequea los marcadores concretos que distinguen una version de la otra, tanto en el HTML
como en la respuesta de /api/data. Correr despues de cada deploy.
"""
import os
import sys

os.environ['DATABASE_URL'] = 'sqlite:////tmp/verif.db'
_AQUI = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _AQUI)
os.chdir(_AQUI)
if os.path.exists('/tmp/verif.db'):
    os.remove('/tmp/verif.db')

from app import app, ensure_tables  # noqa

with app.app_context():
    ensure_tables()

c = app.test_client()
with c.session_transaction() as s:
    s['user_email'] = 'test@ypf.com'; s['user_nivel'] = 1

fallos = []


def check(desc, cond, detalle=''):
    print(f'  [{"OK " if cond else "MAL"}] {desc}{(" — " + detalle) if detalle and not cond else ""}')
    if not cond:
        fallos.append(desc)


print('--- Marcadores del build NUEVO (deben estar todos)')
html = c.get('/mapa').get_data(as_text=True)
check('El mapa expone el enlace a Proyecciones', 'proyecciones-link' in html)
check('El mapa lo posiciona en la barra inferior', 'proyeccionesLink' in html)
check('Existe la funcion de asignacion por ocupacion', 'resolverAvion' in html)
check('El tooltip usa el texto nuevo', 'Avión asignado:' in html)
check('El selector de avion usa la flota calibrada', 'en_escalera' in html)

print('\n--- Restos del build ANTERIOR (no debe quedar ninguno)')
check('Sin "Narrowbody típico"', 'Narrowbody típico' not in html,
      'quedó el texto de classify_heuristic()')
check('Sin el ajuste duplicado del tooltip', 'según ocupación (' not in html)
check('Sin computeConsumption()', 'function computeConsumption' not in html)
check('Sin computeOcupAdjustFactor()', 'computeOcupAdjustFactor' not in html)
check('Sin PHASE_PARAMS', 'PHASE_PARAMS' not in html)

print('\n--- Cabeceras anti-cache en el HTML del mapa')
r = c.get('/mapa')
cc = r.headers.get('Cache-Control', '')
check('map.html no se cachea', 'no-store' in cc, f'Cache-Control = {cc!r}')

print('\n--- Datos del modelo en el servidor')
d = c.get('/api/data').get_json()
md = d.get('modelo_datos', {})
check('Version reportada', d.get('version') is not None, str(d.get('version')))
check('flota.json cargado', md.get('tipos_en_flota', 0) > 0,
      'falta flota.json en el servidor')
check('consumo_rutas.json cargado', md.get('rutas_con_consumo_real', 0) > 0,
      'falta consumo_rutas.json en el servidor')
check('Las rutas traen opciones de avion',
      all(len(m) >= 13 and isinstance(m[12], list) for m in d['cabotaje']['meta'][:20]))
print(f"      version={d.get('version')!r} tipos={md.get('tipos_en_flota')} "
      f"rutas_reales={md.get('rutas_con_consumo_real')}")

print('\n--- La portada y el ruteo del mapa')
portada = c.get('/')
ph = portada.get_data(as_text=True)
check('/ da 200', portada.status_code == 200, f'status {portada.status_code}')
check('La portada trae el logo de YPF', '/static/ypf-aviacion.png' in ph)
check('La portada linkea al mapa en /mapa', 'href="/mapa"' in ph)
check('La portada no se cachea', 'no-store' in portada.headers.get('Cache-Control', ''))
check('El mapa dejo de estar en /', 'id="map"' not in ph,
      'la raiz sigue devolviendo el mapa en vez de la portada')
check('El logo del mapa vuelve a la portada', 'id="logo-link"' in html)
# Nivel 1 no tiene que ver el cuadrado de Admin: /admin lo redirige igual, pero
# ofrecer un acceso y despues negarlo es peor que no ofrecerlo.
c1 = app.test_client()
with c1.session_transaction() as s1:
    s1['user_email'] = 'nivel1@ypf.com'
    s1['user_nivel'] = 1
check('Nivel 1 no ve el cuadrado de Admin',
      'href="/admin"' not in c1.get('/').get_data(as_text=True))

print('\n--- La pagina de proyecciones responde')
r = c.get('/proyecciones')
check('/proyecciones da 200', r.status_code == 200, f'status {r.status_code}')
check('/proyecciones trae el grafico', 'id="chart"' in r.get_data(as_text=True))

print('\n--- El caso concreto del screenshot: Aeroparque-Río Cuarto, 50,1 pax/vuelo')
import avion_model as am  # noqa
from geocode import COORDS  # noqa
info = am.get_aircraft_info('Aeroparque', 'Río Cuarto', 'cabotaje', COORDS, pax_por_vuelo=50.1)
print(f"      asigna: {info['avion']} ({info['asientos']} asientos) · "
      f"{info['consumo_total_kg']:.0f} kg ({info['consumo_total_m3']:.2f} m3)")
check('Ya no asigna un narrowbody de 170 asientos', (info['asientos'] or 0) <= 110,
      f"asigna {info['avion']} de {info['asientos']} asientos")

print()
if fallos:
    print(f'RESULTADO: {len(fallos)} verificacion(es) fallaron')
    for f in fallos:
        print('  -', f)
    sys.exit(1)
print('RESULTADO: build nuevo confirmado, todo en orden')
