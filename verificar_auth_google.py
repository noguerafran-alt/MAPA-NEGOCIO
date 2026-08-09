"""Verifica el login con Google OAuth y los 3 niveles de acceso (mapa/proyecciones/
aerolíneas -> nivel 1, +precios+admin -> nivel 2, +gestión de usuarios -> nivel 3).

Cubre: bloqueo total sin sesión, el callback de OAuth (mockeando la respuesta de Google,
sin hablar con Google de verdad), rechazo de emails no autorizados/no verificados/cuentas
desactivadas, que cada nivel vea exactamente lo que le corresponde, las guardas de
auto-bloqueo en /admin/usuarios/*, y el comando CLI de bootstrap.

    python3 verificar_auth_google.py
"""
import os
import sys
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
os.environ['DATABASE_URL'] = 'sqlite:////tmp/verif_auth_google.db'
os.environ.setdefault('GOOGLE_CLIENT_ID', 'fake-client-id')
os.environ.setdefault('GOOGLE_CLIENT_SECRET', 'fake-client-secret')
if os.path.exists('/tmp/verif_auth_google.db'):
    os.remove('/tmp/verif_auth_google.db')

fallos = []


def check(desc, cond, detalle=''):
    print(f'  [{"OK " if cond else "MAL"}] {desc}{(" — " + str(detalle)) if detalle and not cond else ""}')
    if not cond:
        fallos.append(desc)


from app import app, ensure_tables, google_oauth  # noqa
from models import User, db  # noqa

with app.app_context():
    ensure_tables()
    for email, nivel in [('nivel1@ypf.com', 1), ('nivel2@ypf.com', 2), ('nivel3@ypf.com', 3)]:
        if not User.query.filter_by(email=email).first():
            db.session.add(User(email=email, nombre=email.split('@')[0], nivel=nivel, is_active=True))
    db.session.commit()


def sesion_como(email, nivel):
    c = app.test_client()
    with c.session_transaction() as s:
        s['user_email'] = email
        s['user_nivel'] = nivel
        s['user_nombre'] = email
    return c


# =========================================================================================
print('=== 1. Sin sesión: todo bloqueado ===')
c0 = app.test_client()
check('GET / redirige a /login', c0.get('/').status_code == 302)
check('GET /proyecciones redirige a /login', c0.get('/proyecciones').status_code == 302)
check('GET /aerolineas redirige a /login', c0.get('/aerolineas').status_code == 302)
check('GET /admin redirige a /login', c0.get('/admin').status_code == 302)
check('GET /api/data -> 401 JSON', c0.get('/api/data').status_code == 401)
check('GET /login sirve la página (200)', c0.get('/login').status_code == 200)
check('El botón de Google apunta a /login/google', b'/login/google' in c0.get('/login').data)


# =========================================================================================
print('\n=== 2. Callback de Google (mockeado) ===')
c1 = app.test_client()
token_ok = {'userinfo': {'email': 'Nivel2@YPF.com', 'email_verified': True, 'name': 'Nivel 2'}}
with patch.object(google_oauth, 'authorize_access_token', return_value=token_ok):
    r = c1.get('/login/google/callback')
check('Redirige a / tras login exitoso', r.status_code == 302 and r.headers['Location'].endswith('/'))
with c1.session_transaction() as s:
    check('Nivel correcto en sesión', s.get('user_nivel') == 2, s.get('user_nivel'))
    check('Email normalizado a minúsculas', s.get('user_email') == 'nivel2@ypf.com', s.get('user_email'))
check('Con la sesión ya puesta, entra a /admin', c1.get('/admin').status_code == 200)

c2 = app.test_client()
token_no_autorizado = {'userinfo': {'email': 'intruso@gmail.com', 'email_verified': True}}
with patch.object(google_oauth, 'authorize_access_token', return_value=token_no_autorizado):
    r = c2.get('/login/google/callback')
check('Email no autorizado: redirige a /login con error', r.status_code == 302 and '/login' in r.headers['Location'])
with c2.session_transaction() as s:
    check('No queda sesión activa', 'user_nivel' not in s)

c3 = app.test_client()
token_no_verificado = {'userinfo': {'email': 'nivel1@ypf.com', 'email_verified': False}}
with patch.object(google_oauth, 'authorize_access_token', return_value=token_no_verificado):
    r = c3.get('/login/google/callback')
check('Email no verificado por Google: rechaza aunque esté en la lista',
      r.status_code == 302 and '/login' in r.headers['Location'])

with app.app_context():
    u = User.query.filter_by(email='nivel1@ypf.com').first()
    u.is_active = False
    db.session.commit()
c4 = app.test_client()
token_desactivado = {'userinfo': {'email': 'nivel1@ypf.com', 'email_verified': True}}
with patch.object(google_oauth, 'authorize_access_token', return_value=token_desactivado):
    r = c4.get('/login/google/callback')
check('Cuenta desactivada: rechaza', r.status_code == 302 and '/login' in r.headers['Location'])
with app.app_context():
    u = User.query.filter_by(email='nivel1@ypf.com').first()
    u.is_active = True
    db.session.commit()

c5 = app.test_client()
with patch.object(google_oauth, 'authorize_access_token', side_effect=Exception('boom')):
    r = c5.get('/login/google/callback')
check('Si falla el canje de token con Google, no tira 500', r.status_code == 302 and '/login' in r.headers['Location'])


# =========================================================================================
print('\n=== 3. Nivel 1: mapa/proyecciones/aerolíneas sí, admin y precios no ===')
n1 = sesion_como('nivel1@ypf.com', 1)
check('GET / -> 200', n1.get('/').status_code == 200)
check('GET /proyecciones -> 200', n1.get('/proyecciones').status_code == 200)
check('GET /admin redirige (nivel insuficiente)', n1.get('/admin').status_code == 302)
r = n1.get('/api/data')
check('GET /api/data -> 200', r.status_code == 200)
check('fuel_access = False', r.get_json().get('fuel_access') is False, r.get_json().get('fuel_access'))
check('/admin/usuarios/crear rechaza nivel 1', n1.post('/admin/usuarios/crear', data={}).status_code == 401)


# =========================================================================================
print('\n=== 4. Nivel 2: admin sí, precios desbloqueados, gestión de usuarios no ===')
n2 = sesion_como('nivel2@ypf.com', 2)
check('GET /admin -> 200', n2.get('/admin').status_code == 200)
r = n2.get('/api/data')
check('fuel_access = True', r.get_json().get('fuel_access') is True, r.get_json().get('fuel_access'))
check('/admin/usuarios/crear rechaza nivel 2', n2.post('/admin/usuarios/crear', data={}).status_code == 401)
html2 = n2.get('/admin').get_data(as_text=True)
check('La sección "Usuarios" NO aparece para nivel 2', 'Usuarios (acceso con Google' not in html2)


# =========================================================================================
print('\n=== 5. Nivel 3: todo, incluida la gestión de usuarios ===')
n3 = sesion_como('nivel3@ypf.com', 3)
html3 = n3.get('/admin').get_data(as_text=True)
check('La sección "Usuarios" SÍ aparece para nivel 3', 'Usuarios (acceso con Google' in html3)

r = n3.post('/admin/usuarios/crear', data={'email': 'nuevo@ypf.com', 'nombre': 'Nuevo', 'nivel': '2'})
check('Crear usuario nuevo funciona', r.status_code == 200 and r.get_json().get('ok'), r.get_json())
with app.app_context():
    nuevo = User.query.filter_by(email='nuevo@ypf.com').first()
    check('Quedó guardado con el nivel correcto', nuevo is not None and nuevo.nivel == 2)
    nuevo_id = nuevo.id

r = n3.post(f'/admin/usuarios/{nuevo_id}/nivel', data={'nivel': '1'})
check('Cambiar nivel de OTRA cuenta funciona', r.status_code == 200, r.get_json())
r = n3.post(f'/admin/usuarios/{nuevo_id}/toggle_activo')
check('Desactivar OTRA cuenta funciona', r.status_code == 200 and r.get_json().get('is_active') is False, r.get_json())

with app.app_context():
    yo_id = User.query.filter_by(email='nivel3@ypf.com').first().id
r = n3.post(f'/admin/usuarios/{yo_id}/nivel', data={'nivel': '2'})
check('No puede bajarse su PROPIO nivel de 3 a 2', r.status_code == 400, r.get_json())
r = n3.post(f'/admin/usuarios/{yo_id}/toggle_activo')
check('No puede desactivarse a sí mismo', r.status_code == 400, r.get_json())

r = n3.post('/admin/usuarios/crear', data={'email': 'x', 'nivel': '1'})
check('Rechaza email inválido', r.status_code == 400, r.get_json())
r = n3.post('/admin/usuarios/crear', data={'email': 'nivel3@ypf.com', 'nivel': '1'})
check('Rechaza email duplicado', r.status_code == 400, r.get_json())
r = n3.post('/admin/usuarios/crear', data={'email': 'otromas@ypf.com', 'nivel': '9'})
check('Rechaza nivel fuera de rango', r.status_code == 400, r.get_json())


# =========================================================================================
print('\n=== 6. Comando CLI de bootstrap (`flask autorizar-usuario`) ===')
runner = app.test_cli_runner()
result = runner.invoke(args=['autorizar-usuario', '--email', 'Boss@YPF.com', '--nombre', 'Boss', '--nivel', '3'])
check('Sale con código 0', result.exit_code == 0, result.output)
with app.app_context():
    u = User.query.filter_by(email='boss@ypf.com').first()
    check('Se creó con el email en minúsculas', u is not None)
    check('Nivel correcto', u is not None and u.nivel == 3)

result2 = runner.invoke(args=['autorizar-usuario', '--email', 'boss@ypf.com', '--nombre', 'Boss', '--nivel', '1'])
with app.app_context():
    total = User.query.filter_by(email='boss@ypf.com').count()
    u = User.query.filter_by(email='boss@ypf.com').first()
    check('Re-correrlo actualiza en vez de duplicar', total == 1, total)
    check('El nivel se actualizó', u is not None and u.nivel == 1)


# =========================================================================================
print('\n=== 7. logout limpia toda la sesión ===')
c6 = sesion_como('nivel2@ypf.com', 2)
check('Antes de logout, entra a /admin', c6.get('/admin').status_code == 200)
c6.get('/logout')
check('Después de logout, /admin vuelve a redirigir', c6.get('/admin').status_code == 302)


# =========================================================================================
print('\n' + '=' * 70)
if fallos:
    print(f'{len(fallos)} CHEQUEO(S) FALLIDO(S):')
    for f in fallos:
        print('  -', f)
    sys.exit(1)
print('TODO OK')
