# Rutas Aéreas Argentinas — Mapa interactivo

App Flask + Postgres que muestra un mapa mundial interactivo de rutas aéreas
argentinas (cabotaje e internacional) con vuelos, pasajeros y ocupación por
ruta, a partir de las planillas "series históricas" de ANAC.

## Uso

- `/` — el mapa, `/proyecciones` y `/aerolineas` — requieren estar logueado
  con una cuenta de Google autorizada (nivel 1 o más).
- `/admin` — panel de administración; requiere nivel 2 o más. Los
  precios/ingresos de combustible también se desbloquean recién en nivel 2.
- `/admin` → sección **Usuarios** — dar de alta o editar cuentas; sólo
  visible y usable para nivel 3.

No hay contraseñas: la identidad la verifica Google, y la app sólo decide
qué nivel tiene cada email según una lista blanca (tabla `User`). Un email
que no esté en esa lista puede loguearse con Google igual, pero la app lo
rechaza con "cuenta no autorizada".

## Configurar Google OAuth (una sola vez)

1. En [Google Cloud Console](https://console.cloud.google.com/apis/credentials),
   creá un **OAuth 2.0 Client ID** de tipo "Aplicación web".
2. En **Authorized redirect URIs**, agregá:
   `https://<tu-dominio-de-render>.onrender.com/login/google/callback`
   (y `http://localhost:5000/login/google/callback` si vas a probar en local).
3. Copiá el **Client ID** y el **Client Secret** — van en las variables de
   entorno `GOOGLE_CLIENT_ID` y `GOOGLE_CLIENT_SECRET` (paso 3 de Deploy).

## Disco de Render (en vez de Neon)

La app usa SQLite en el disco persistente `/var/data/mapa.db` (1 GB) cuando ese
path existe y se puede escribir. Neon queda solo como origen para copiar una vez.

1. En Render: servicio web → **Disks** → disco montado en `/var/data` (1 GB). Plan Starter o superior (el free no tiene disco persistente).
2. Dejá `DATABASE_URL` con la URL de Neon en Environment. Opcional: copiá la misma URL a `NEON_DATABASE_URL`.
3. Deploy. Al arrancar, si el SQLite está vacío, copia todas las tablas (incluye los archivos de `/admin`) al disco.
4. Entrá al mapa y a `/admin` y confirmá que se ven tus datos.
5. Recién ahí podés **borrar el servicio/base de Neon** y la variable `DATABASE_URL` de Postgres.

Si la copia automática no corrió, desde el **Shell** de Render:

```
flask migrar-de-neon
```

## Deploy en Render

1. Subí este repo a GitHub.
2. En Render: **New + → Blueprint**, elegí este repo. `render.yaml` crea
   el servicio web con disco persistente en `/var/data` (SQLite). Ya no usa Neon.
3. En la pestaña **Environment** del servicio web, cargá a mano
   `GOOGLE_CLIENT_ID` y `GOOGLE_CLIENT_SECRET` (salen del paso anterior).
4. **Autorizá tu propia cuenta** desde el Shell de Render (pestaña Shell del
   servicio, o `render ssh` desde tu máquina):
   ```
   flask autorizar-usuario --email vos@ypf.com --nombre "Tu Nombre" --nivel 3
   ```
   Es el único paso manual: de ahí en adelante, se pueden autorizar más
   cuentas desde `/admin` → **Usuarios**, sin volver a tocar la terminal.
5. Entrá a `/login`, iniciá sesión con esa cuenta de Google, y listo.

Si venís de una versión anterior con `ADMIN_PASSWORD`/`MAP_PASSWORD`/
`FUEL_PASSWORD`: esas tres variables ya no se usan. Corré el paso 4 antes de
que se pierda el acceso, y después podés borrarlas del dashboard de Render.

## Actualizar datos más adelante

Cada vez que ANAC publique una planilla nueva, subila desde `/admin`. Los
meses/años que ya existen se actualizan (se pisan), los nuevos se agregan.
Nada se borra ni se duplica.
