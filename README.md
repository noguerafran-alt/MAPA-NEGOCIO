# Rutas Aéreas Argentinas — Mapa interactivo

App Flask + Postgres que muestra un mapa mundial interactivo de rutas aéreas
argentinas (cabotaje e internacional) con vuelos, pasajeros y ocupación por
ruta, a partir de las planillas "series históricas" de ANAC.

## Uso

- `/` — la portada: el logo y los accesos en cuadrados a todo lo demás. Los
  cuadrados se arman en el servidor según el nivel de la sesión, así que nadie
  ve una puerta que le va a dar un portazo.
- `/mapa` — el mapa de rutas. **Antes estaba en `/`**; si algo apunta a la raíz
  esperando el mapa, ahora cae en la portada.
- `/proyecciones`, `/aerolineas`, `/noticias` y `/modelo` — requieren estar
  logueado con una cuenta de Google autorizada (nivel 1 o más), igual que el mapa.
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

El mapa puede mostrar **12 meses hacia adelante**: se estiman vuelos y pasajeros
por ruta y se inyectan en `/api/data` como períodos más. El modelo **no corre en
el servidor** — la instancia tiene 512 MB y no entra; corre en tu máquina y deja
el resultado en `proyeccion_precomputada.json`, que es lo que sirve el deploy. No proyecta combustible — el mapa ya sabe pasar de vuelos+pasajeros a m³
(elige avión por ocupación y multiplica por el consumo por vuelo), así que toda
la cadena de consumo se calcula sola río abajo.

El método está explicado para el usuario en `/modelo`. En resumen: un modelo
global LightGBM que no predice el nivel sino la **corrección sobre el naive
estacional** (el mismo mes del año anterior), porque los árboles no extrapolan.
Mejora al naive de forma real pero moderada — MASE mediano 1.06 contra 1.11 en
pasajeros y 0.98 contra 1.04 en vuelos: sirve para dimensionar, no para
comprometer volumen contractual.

```
proyeccion_datos.py       empalma historical_2001_2022.json con route_monthly
                          (el JSON trae los pasajeros en MILES, la base en unidades)
proyeccion_modelo.py      features, modelo y backtest contra baselines
proyeccion_forecast.py    entrena y arma las filas — necesita pandas + lightgbm
precomputar_proyeccion.py corre el modelo y guarda el resultado (en tu máquina)
proyeccion_archivo.py     lee ese archivo en el server — stdlib pura, sin pandas
templates/modelo.html     la explicación que ve el usuario
```

### Por qué el servidor no entrena

Entrenar no entra en la instancia. Medido con un solo hilo, que es lo que se
parece a los 0.5 CPU de Render:

| | ahora (lee el archivo) | entrenando en el proceso web |
|---|---|---|
| RSS sirviendo `/api/data` | 153 MB de pico | ~284 MB, pico 377 MB |
| primer `/api/data` | ~1 s | ~12 s a un hilo → 25-30 s a 0.5 CPU |
| después de cada reinicio | ~1 s | vuelve a pagar esos 25-30 s |

El costo fijo es lo que más duele: importar numpy y pandas son 50 MB de RSS y
lightgbm otros 70 MB, **antes** de tocar un solo dato. Con 512 MB en total —y
ahí adentro también entran Flask, el histórico 2001-2022, el ORM y el JSON que
arma el mapa— eso deja al servicio a un upload de `/admin` de distancia del OOM
killer. Y el caché del modelo vive en memoria, así que se pierde en cada
reinicio y la primera visita siguiente vuelve a esperar medio minuto.

Por eso `requirements.txt` **no** instala numpy/pandas/lightgbm, y el deploy
sirve `proyeccion_precomputada.json` (~300 KB) leyéndolo con la stdlib.

### Regenerar la proyección (una vez por planilla nueva)

```
pip install -r requirements-modelo.txt      # sólo la primera vez
python precomputar_proyeccion.py
git add proyeccion_precomputada.json
git commit -m "Proyección al <mes>" && git push
```

Si los datos frescos están en la instancia local, apuntá ahí la base:

```
python precomputar_proyeccion.py --db ../MAPA-NEGOCIO-LOCAL/instance/local_dev.db
```

Mientras no se regenere no se rompe nada, pero tampoco se disimula:

- Un mes proyectado que **ya tiene dato real** no se dibuja. Si no, el mapa
  sumaría la proyección encima de la planilla de ANAC y ese mes contaría doble.
- `/modelo` avisa "se calculó con datos hasta X y la base ya llega hasta Y", con
  la fecha exacta en que se generó el archivo.
- `/api/deploy_status` trae un bloque `proyeccion` con lo mismo en JSON — y si
  la proyección no está, el error exacto en vez de una suposición.

Para medir de nuevo el backtest (tarda varios minutos, no entra en un request):

```
python proyeccion_modelo.py
```

## Actualizar datos más adelante

Cada vez que ANAC publique una planilla nueva, subila desde `/admin`. Los
meses/años que ya existen se actualizan (se pisan), los nuevos se agregan.
Nada se borra ni se duplica.

## MS RTIC — consumo por ruta y aerolínea, en vivo

El histórico de ANAC viene agregado por ruta-mes y no dice quién voló. MS RTIC son las
partidas que publica Aeropuertos Argentina, una por una, con aerolínea y matrícula, así
que abre cada ruta en las aerolíneas que la operan y le pone a cada una su consumo. El
combustible lo calcula `avion_model`, el mismo del mapa: no hay un segundo cálculo.

```
sondeo.py       poller cada 5 min, adentro del proceso web
msrtic.py       cruza matrícula -> avión, clasifica proveedor y calcula m3
proveedores.py  quién abastece cada ruta (viene de RADAR-MARKET-SHARE)
/api/msrtic     las filas. NIVEL 1: no es público
```

**Está detrás del login y no es por costumbre.** `datos/proveedores.json` dice qué rutas
abastece YPF y cuáles Axion o Raizen, y eso no es información pública. El resto del
módulo sí lo es (el feed de AA2000 y el registro de OpenSky son abiertos), pero el
endpoint entero exige nivel 1 porque el payload los lleva juntos. Los usuarios los crea
el admin, así que detrás del login sólo hay gente de YPF.

**Por qué un hilo y no un cron.** Lo decide `render.yaml`: plan `starter` (no se suspende,
el hilo no duerme), `--workers 1` (una sola instancia, el poller no se duplica) y disco
persistente en `/var/data` (el acumulado sobrevive los deploys). Con eso, un hilo es cero
infraestructura nueva y `aa2000.py` es stdlib pura: no agrega dependencias.

**Si algún día se sube a 2 workers, esto hay que mover a un cron.** Dos pollers sobre la
misma SQLite se pisan, y el síntoma no sería un error sino filas trabadas y sondeos
perdidos.

**Las dos bases van en el disco, no en el repo.** `aa2000_oficial.db` la escribe el
poller; `aircraft_db.sqlite` son 52 MB y se construye una vez desde OpenSky, en
background — si el arranque esperara esa descarga, un deploy tardaría minutos y un fallo
de red dejaría el servicio sin levantar. Mientras no esté, MS RTIC funciona igual con
todos los aviones estimados por el modelo del mapa: pierde precisión, no funcionalidad.

Medido contra el feed en vivo: de 492 partidas, 205 publican matrícula (42%) y 187
terminan con un tipo que el mapa sabe consumir (38%). Las otras se estiman con
`seleccionar_avion`, y cada fila dice cuántos de sus aviones fueron medidos y cuántos
estimados: un total de m³ que no distingue una cosa de la otra no se puede auditar.

### La pantalla "Quién le cargó" y la planilla

`/quien-cargo` (nivel 1, y con acceso desde el landing) es el tablero de partidas con la
petrolera que abasteció cada una — la misma pantalla que el radar sirve en
`127.0.0.1:8600`, traída adentro para no depender de que el radar corra en una PC.

Dos cuentas distintas, y la distinción es del módulo del radar, no de la pantalla: en la
**lista** entran las partidas programadas (un tablero sin ellas sería peor que el del
portal de AA2000), pero en los **porcentajes** sólo las que ya despegaron. Una partida que
todavía no salió no es una carga de combustible, y contarla haría que el número cambie
según la hora del día en que se mire.

**Actualizar la planilla es nivel 2, no 1.** Leer la tabla es una cosa; cambiarla es otra:
decide de qué petrolera es cada ruta en todo el mapa, así que una carga equivocada mueve
números en pantallas que nadie está mirando.

La planilla subida se escribe en `/var/data/proveedores.json`, **nunca junto al código**:
el filesystem de Render es efímero y el próximo deploy la borraría, volviendo sola a la
semilla del repo sin avisar. `msrtic.tabla_proveedores()` prefiere la del disco y cae a
`datos/proveedores.json` (la semilla) sólo si no hay ninguna subida.

Y si el archivo no tiene ninguna ruta reconocible, **la tabla anterior queda como está**:
quedarse sin tabla por un archivo con el formato equivocado es peor que no actualizar.
Verificado con la planilla real (149 rutas) y con un archivo inválido, que da 400 y no
vacía nada.

Lo que lee y escribe la pantalla son los mismos `cargar_excel.leer` y `.escribir_json` que
usa la consola del radar. Con dos lectores, un día uno interpreta una columna distinto y
la tabla pasa a significar cosas distintas según quién la cargó.

### El puente hacia la terminal: el Excel del SharePoint

Esta instancia sondea AA2000 cada 5 minutos, 24/7, y tiene el acumulado bueno. La PC donde
corre el mapa local no puede: arranca sin internet y ahí el feed suele estar bloqueado por
la red (medido: 403 URLBlocked). Así que el dato viaja en un archivo.

`GET /api/msrtic/export.xlsx` (nivel 1) baja el acumulado como Excel, y hay un botón en
`/quien-cargo`. Se pega en la biblioteca de SharePoint y el mapa lo lee de la carpeta
sincronizada con su `importar_ms.py`.

**Lleva las partidas crudas, no los m³.** Si trajera el consumo calculado habría dos
implementaciones del mismo número —la de acá y `avion_model` del mapa— y un día
diferirían sin que nadie sepa cuál creer. Con las crudas hay un solo cálculo, y si mañana
se recalibra la flota el histórico se recalcula solo.

El formato lo define `intercambio_ms.py`, que **está en los dos repos con el mismo
contenido**: con un escritor y un lector separados, un día uno agrega una columna y el
archivo pasa a significar cosas distintas según quién lo abrió.

La hoja `meta` del Excel lleva `generado`, `hasta` y el conteo de filas. No es decoración:
es lo único que le permite a la terminal decir "datos al 8 de septiembre" en vez de
mostrarlos como de hoy.
