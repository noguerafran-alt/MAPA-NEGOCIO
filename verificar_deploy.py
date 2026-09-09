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
# `proyeccionesLink` era la implementacion VIEJA: JS que le calculaba el `right` a cada
# acceso, uno por uno. La reemplazo el sistema de zonas del mapa local -- `medirZonas()`
# mide las franjas y los accesos se acomodan con flexbox -- asi que buscar ese nombre daba
# un falso negativo: el marcador desaparecio porque la implementacion mejoro, no porque la
# barra se rompiera. Verificado en el navegador: los 6 accesos visibles, sin solaparse y
# dentro de la ventana.
check('El mapa acomoda la barra inferior', 'medirZonas' in html and 'barra-inferior' in html)
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


print('\n' + '--- Quien le cargo: el denominador de los porcentajes')
# CON DATOS SINTETICOS a proposito: la base de partidas vive en el disco de Render y este
# repo no la tiene en local. Lo que se verifica no son los datos sino el ENSAMBLADO, que
# es donde estuvo el error: la pantalla junta la LISTA de tablero() -- que incluye las
# programadas -- con los PORCENTAJES de desde_base(), que solo cuenta las despegadas. Con
# el total de la lista pisando al de los porcentajes, el share de YPF se dividia por un
# numero mucho mas grande: 17,1% donde el numero es 67,1%, sumando 32% en vez de 100%.
import app as _appmod  # noqa

_tab = {'vuelos': [{'x': 1}] * 677, 'total': 677, 'ocurridas': 173,
        'origenes': ['AEP'], 'tabla': {'existe': True}, 'n_rutas_en_tabla': 9}
_res = {'vuelos': [{'x': 1}] * 173, 'total': 173,
        'por_proveedor': {'YPF': 116, 'RAIZEN': 37, 'AXION': 16},
        'n_sin_resolver': 4, 'por_motivo': {}, 'por_ruta': [], 'n_rutas': 0,
        'rutas_sin_declarar': {}, 'programadas_sin_ocurrir': 131,
        'denominador_inconsistente': None}

_d = _appmod._quien_cargo_plano(_tab, _res, {'activo': True}, None)
_partes = sum(_d['por_proveedor'].values()) + _d['n_sin_resolver']
check('El denominador es el de las partidas medidas, no el de la lista',
      _d['total'] == 173, "total=%s (la lista tiene 677)" % _d['total'])
check('Los porcentajes cierran en 100%', _partes == _d['total'],
      "las partes suman %s y el denominador es %s" % (_partes, _d['total']))
check('El pie de la lista sigue contando las programadas',
      _d['n_vuelos_total'] == 677, "n_vuelos_total=%s" % _d['n_vuelos_total'])
check('Sin mezcla, no hay aviso de denominador',
      not _d.get('denominador_inconsistente'))

# Y si alguien vuelve a mezclar, la pantalla tiene que decirlo. Una guarda que no puede
# fallar es peor que ninguna: da la sensacion de estar cubierto.
_mal = _appmod._quien_cargo_plano(_tab, dict(_res, total=677), {'activo': True}, None)
check('Si alguien vuelve a mezclar las cuentas, la pantalla avisa',
      bool(_mal.get('denominador_inconsistente')),
      'no aviso nada con el denominador en 677')
print()
if fallos:
    print(f'RESULTADO: {len(fallos)} verificacion(es) fallaron')
    for f in fallos:
        print('  -', f)
    sys.exit(1)
print('RESULTADO: build nuevo confirmado, todo en orden')
