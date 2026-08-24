# Rutas Aéreas Argentinas — Mapa interactivo

App Flask + Postgres que muestra un mapa mundial interactivo de rutas aéreas
argentinas (cabotaje e internacional) con vuelos, pasajeros y ocupación por
ruta, a partir de las planillas "series históricas" de ANAC.

## Uso

- `/` — el mapa, `/proyecciones`, `/aerolineas` y `/modelo` — requieren estar
  logueado con una cuenta de Google autorizada (nivel 1 o más).
- `/modelo` — explica cómo se arma la proyección de tráfico que llena los
  períodos futuros del mapa, con las cifras del backtest.
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

## Proyección de tráfico (opcional)

El mapa puede mostrar **12 meses hacia adelante**: `proyeccion_forecast.py`
estima vuelos y pasajeros por ruta y los inyecta en `/api/data` como períodos
más. No proyecta combustible — el mapa ya sabe pasar de vuelos+pasajeros a m³
(elige avión por ocupación y multiplica por el consumo por vuelo), así que toda
la cadena de consumo se calcula sola río abajo.

El método está explicado para el usuario en `/modelo`. En resumen: un modelo
global LightGBM que no predice el nivel sino la **corrección sobre el naive
estacional** (el mismo mes del año anterior), porque los árboles no extrapolan.
Mejora al naive de forma real pero moderada — MASE mediano 1.06 contra 1.11 en
pasajeros y 0.98 contra 1.04 en vuelos: sirve para dimensionar, no para
comprometer volumen contractual.

```
proyeccion_datos.py     empalma historical_2001_2022.json con route_monthly
                        (el JSON trae los pasajeros en MILES, la base en unidades)
proyeccion_modelo.py    features, modelo y backtest contra baselines
proyeccion_forecast.py  filas listas para /api/data, con caché en memoria
templates/modelo.html   la explicación que ve el usuario
```

**Las dependencias no están en `requirements.txt` a propósito.** El import está
guardado: sin `pandas` + `lightgbm` la app arranca igual y el mapa funciona
completo, sólo que sin períodos futuros. Medido en local con la base de
desarrollo:

| | sin proyección | con proyección |
|---|---|---|
| RSS después de `/api/data` | ~135 MB | ~284 MB (pico 377 MB) |
| primer `/api/data` | ~1 s | ~13 s (entrena los dos modelos) |
| llamadas siguientes | ~1 s | ~1 s (caché en memoria) |

En una instancia de 512 MB eso deja poco margen, que es justo el problema que
ya provocó 502 en `/admin`. Para activarla en Render hay que agregar
`pandas` y `lightgbm` a `requirements.txt` y subir el plan de la instancia; si
después aparecen 502, sacarlas alcanza para volver atrás sin tocar nada más.

El caché se invalida solo: la clave incluye el último período cargado y la
cantidad de filas con dato, así que subir una planilla nueva desde `/admin`
fuerza el reentrenamiento sin que haya que acordarse de limpiarlo.

Para medir de nuevo el backtest (tarda varios minutos, no entra en un request):

```
python proyeccion_modelo.py
```

## Actualizar datos más adelante

Cada vez que ANAC publique una planilla nueva, subila desde `/admin`. Los
meses/años que ya existen se actualizan (se pisan), los nuevos se agregan.
Nada se borra ni se duplica.
