# Manual de operacion

Referencia de uso diario del bot actual. Para la vision general ve a
[`README.md`](README.md); para instalar en otra computadora, a
[`GUIA_SOCIOS_MARKETPLACE_BOT.md`](GUIA_SOCIOS_MARKETPLACE_BOT.md).

## Indice

1. [Arranque](#arranque)
2. [Niveles de autonomia](#niveles-de-autonomia)
3. [Calendario y horarios](#calendario-y-horarios)
4. [Proteccion de cuenta](#proteccion-de-cuenta)
5. [Resultados de publicacion inciertos](#resultados-de-publicacion-inciertos)
6. [Reintentos](#reintentos)
7. [Fotos por cuenta](#fotos-por-cuenta)
8. [Varias cuentas](#varias-cuentas)
9. [Productos fuera del inventario](#productos-fuera-del-inventario)
10. [Fuentes de inventario](#fuentes-de-inventario)
11. [Descripciones con IA](#descripciones-con-ia)
12. [Herramientas de linea de comandos](#herramientas-de-linea-de-comandos)
13. [Archivos y variables de entorno](#archivos-y-variables-de-entorno)
14. [Codigos de alerta](#codigos-de-alerta)

## Arranque

```powershell
python -m pip install -r requirements_marketplace_bot.txt
copy marketplace_accounts.example.json marketplace_accounts.json   # editar con tus cuentas
powershell -ExecutionPolicy Bypass -File .\abrir_sesion_marketplace_cuenta.ps1   # una vez por cuenta
powershell -ExecutionPolicy Bypass -File .\iniciar_panel_marketplace.ps1         # panel
powershell -ExecutionPolicy Bypass -File .\instalar_trabajador_marketplace.ps1   # trabajador permanente
```

- `abrir_sesion_marketplace_cuenta.ps1 -Account cuenta2 -Port 9223` abre Chrome
  con el perfil de esa cuenta y su puerto de depuracion. Inicia sesion en
  Facebook y deja la ventana abierta. El script desactiva el "backgrounding" de
  Chrome para que el formulario no se congele con la ventana tapada.
- `marketplace_accounts.json`: una clave por cuenta con `display_name`,
  `debugger_address` (un puerto distinto por cuenta: 9222, 9223...) y
  `chrome_profile`. No se versiona.
- `instalar_trabajador_marketplace.ps1` crea la tarea de Windows
  `MarketplaceBotScheduler`, que arranca al iniciar sesion, se reintenta hasta 3
  veces si cae y escribe en `marketplace_scheduler_worker.log`. El interruptor
  `Bot activo` viene apagado: el trabajador corre pero no publica.

### Panel

`iniciar_panel_marketplace.ps1` lo levanta en el puerto **8794** (parametro
`-Port`; el valor por defecto de `marketplace_bot_dashboard.py` solo es 8787 si
lo ejecutas directamente). Pestanas: `Nueva publicacion`, `Automatizacion`,
`Actividad`, `Inventario`, `Cuentas`, `Avanzado`, `Diagnostico`.

El panel controla el bot entero, asi que exige una llave. Al arrancar genera un
token nuevo, lo guarda en `marketplace_dashboard_token.txt` y lo agrega al
enlace que abre el script. Sin ese enlace responde `No autorizado`; el token
vive solo mientras el proceso este abierto. Solo escucha en `127.0.0.1`: un
`--host` distinto falla salvo que agregues `--allow-remote` a proposito. Un
segundo panel en el mismo puerto no arranca (no reutiliza el puerto en Windows),
asi que nunca pisa el token del primero.

## Niveles de autonomia

`Automatizacion > Modo`. El valor interno esta entre parentesis.

| Nivel | Que hace |
|---|---|
| `Simulacion` (`simulation`) | Valida el plan sin abrir Facebook |
| `Prueba sin publicar` (`dry_run`) | Llena el formulario real y se detiene antes de publicar |
| `Supervisado` (`supervised`) | Igual, pero cada elemento exige aprobacion |
| `Calendario aprobado` (`semiautomatic`) | Publica solo lo aprobado |
| `Autonomo` (`autonomous`) | Aprueba y publica el calendario sin intervencion |

Solo `Calendario aprobado` y `Autonomo` publican de verdad. Sube de nivel solo
cuando cada cuenta complete varias pruebas correctas en el anterior.

## Calendario y horarios

`Generar semana` crea el calendario sin publicar nada. Reglas (valores por
defecto en `marketplace_scheduler.py`):

| Regla | Defecto | Notas |
|---|---|---|
| Dias activos | lunes a sabado | |
| Horario | 09:00 a 18:00 | |
| Maximo diario | 3 | por calendario, ademas del limite por cuenta |
| Intervalo | 180 min | entre anuncios |
| Horizonte | 7 dias | |
| Descanso por producto | 3 dias | no repite el producto antes |
| Variacion del horario | 7 min | 0 a 30; ver abajo |

La **variacion** mueve cada publicacion unos minutos al azar para que el patron
no sea siempre identico. Es estable (generar de nuevo la misma semana da los
mismos horarios) y nunca solapa dos espacios seguidos.

La rotacion elige el producto que lleva mas tiempo sin publicarse y alterna
familias. Los trabajos ya programados no se alteran al regenerar.

## Proteccion de cuenta

Antes de cada anuncio real `marketplace_safety.py` aplica reglas por cuenta.
Se configuran en `Automatizacion > Reglas > Proteccion de cuenta` con tres
niveles preestablecidos (o `custom`):

| Nivel | Al dia | En 7 dias | Separacion minima | Entre cuentas | Repetir familia |
|---|---|---|---|---|---|
| `conservative` | 2 | 8 | 180 min | 120 min | 10 dias |
| `balanced` (defecto) | 3 | 12 | 120 min | 90 min | 7 dias |
| `active` | 5 | 20 | 90 min | 60 min | 5 dias |

Ademas:

- **Calentamiento** (14 dias por defecto): una cuenta nueva publica 1 anuncio al
  dia la primera mitad del periodo y 2 la segunda.
- **Pausa por fallos**: tras 2 fallos seguidos que dicen algo de la cuenta o del
  formulario, descansa 12 horas. Un SGI caido o una foto repetida no cuentan.
- **Pausa por restriccion**: si Facebook muestra un aviso de limite o
  restriccion, la cuenta se pausa 72 horas (alerta `ACCOUNT_RESTRICTED`).
- **Revision del texto**: bloquea anuncios con enlaces o correos y avisa de
  telefonos, mayusculas y titulos largos.
- **Variacion de redaccion** por cuenta y semana.

Lo que frena la proteccion se reprograma sin gastar intentos. Puedes pausar o
reanudar una cuenta a mano desde el panel. El objetivo es publicar menos y
mejor; no intenta esconder la automatizacion.

## Resultados de publicacion inciertos

Justo antes de pulsar `Publish`, el publicador deja un archivo en
`marketplace_publish_intents/`. Si despues el proceso se corta (timeout, cierre
inesperado, reinicio de Windows), el trabajador ve ese archivo y marca el
trabajo `blocked` con la alerta `PUBLISH_OUTCOME_UNKNOWN`. **No lo reintenta**:
el anuncio pudo haberse creado y reintentar lo duplicaria.

Para resolverlo, revisa Marketplace:

- Si el anuncio **no existe**, pulsa `Reintentar` en el calendario (borra la
  marca y vuelve a encolar).
- Si **existe**, cancela el elemento.

Cuando una publicacion se confirma, el bot guarda la URL en `listing_url`,
visible en el historial.

## Reintentos

Un fallo temporal no se reintenta siempre a la misma distancia: cada intento
duplica la espera desde `retry_delay_minutes` (30 por defecto), con tope de 6
horas y una variacion de mas o menos 20 %. `max_attempts` (2 por defecto,
maximo 5) agotado deja el trabajo `blocked`. Captcha, 2FA, revision de cuenta o
sesion cerrada bloquean sin reintentar.

## Fotos por cuenta

En `Automatizacion > Fotos definidas por producto y cuenta` marcas las miniaturas
que usara cada producto en cada cuenta y pulsas `Guardar reglas`. Se aplica al
siguiente calendario; lo ya programado conserva sus fotos.

Debajo de las miniaturas aparece `Orden de subida` (con 2 o mas fotos marcadas):
mueve cada foto con las flechas `←` `→`. **La primera es la portada** y ese es el
orden en que se suben a Facebook. Las fotos que marcas despues se agregan al
final. El orden es propio de cada producto y cuenta.

El bot calcula una huella SHA-256 del **contenido** de cada foto:

- Una foto publicada en una cuenta no vuelve a programarse en esa cuenta; en
  otra cuenta si puede usarse.
- Renombrar el archivo no lo convierte en una foto nueva.
- Si todas las fotos de un producto ya se usaron en una cuenta, el calendario lo
  omite para ella. Si aun asi llega al publicador, queda `blocked` con
  `IMAGE_REUSED`. Para repetir el producto hacen falta fotos reales nuevas.
- Con la conexion a Drive solo se guarda una copia por color, y se ofrecen todas
  las fotos distintas de cada familia sin arrastrar fotos viejas.

El historial empieza con las publicaciones del nuevo trabajador; las antiguas
que no guardaron sus rutas no se reconstruyen.

## Varias cuentas

`Automatizacion > Cuentas activas`: marca una o varias y elige

- `Rotar entre cuentas`: reparte los anuncios alternadamente.
- `Publicar en todas`: crea el mismo anuncio para cada cuenta, separadas siete
  minutos entre si (y por la regla de separacion entre cuentas de la proteccion).

Cada cuenta tiene su sesion de Chrome, candado de ejecucion
(`marketplace_runtime_locks/`), historial de fotos, reintentos y alertas. Para
una publicacion manual, elige la cuenta en `Cuenta de Marketplace` antes de
seleccionar productos.

## Productos fuera del inventario

`Automatizacion > Agregar fuera del inventario`: nombre, precio, categoria,
condicion, ubicacion, etiquetas, descripcion y de 1 a 10 fotos. Sin descripcion
se genera una local. El producto se guarda en `marketplace_custom_products/`
(y en la base) con su propio Excel, y entra en la rotacion sin tocar
`ArticulosGenerados.xlsx`.

## Fuentes de inventario

El bot lee `ArticulosGenerados.xlsx` y la carpeta `imagenes_productos`. Se
generan de dos maneras:

### SGI La Comarca + Google Drive (recomendada)

Descrita en el [README](README.md#conexion-con-el-sgi-y-google-drive-opcional).
Con `Bot activo` encendido el trabajador sincroniza cada hora (a los 10 minutos
si la ultima vez fallo) y no arma una semana con existencias viejas si no puede
sincronizar. Solo publica lo que tiene stock y precio unico de venta; los
productos descartados aparecen en el panel con su motivo. Justo antes de cada
anuncio vuelve a comprobar stock, precio y fotos contra el SGI y omite el
elemento si algo cambio (`OUT_OF_STOCK`, `PRICE_ISSUE`, `NO_PHOTOS`). Si el SGI
no responde, el trabajo se reprograma. Si se revoca la llave, el trabajador deja
de publicar y levanta una alerta.

Comandos utiles (`sgi_sync.py`):

```powershell
python sgi_sync.py --authorize-drive           # autoriza Drive en solo lectura
python sgi_sync.py --refresh-drive             # ignora el indice de Drive guardado
python sgi_sync.py --build-mapping conteo.xlsx # genera sgi_codigos_anteriores.json
```

Los archivos locales de esta integracion (`.sgi_integration_key.bin`,
`token_google_drive.json`, `marketplace_integrations.json`, `sgi_drive_cache/`,
`sgi_drive_index.json`, `sgi_sync_report.json`) no se versionan.

### Fotos locales de respaldo

Si un producto no tiene fotos en Drive, el bot usa las de la carpeta local
`imagenes_productos/<CODIGO>/` (hasta 10, ordenadas por nombre). Si no hay en
ninguno de los dos sitios, el producto se descarta con `NO_PHOTOS`.

## Descripciones con IA

Opcional; sin clave el bot usa redaccion comercial local. Se configura desde el
panel (`/api/ai/configure`): la clave se guarda cifrada con DPAPI en
`.openai_key.bin`, ligada al usuario de Windows, y no se copia a otra
computadora. El modelo por defecto sale de `OPENAI_MODEL`. Tambien se lee
`OPENAI_API_KEY` si esta definida.

## Herramientas de linea de comandos

El panel cubre el uso normal. Estas piezas sirven para diagnostico y scripts.

| Comando | Para que |
|---|---|
| `python marketplace_scheduler_worker.py [--once] [--poll-seconds 30]` | Trabajador en primer plano |
| `python marketplace_scheduler.py --generate --show` | Genera y muestra el calendario (`--approve`, `--cancel-future`) |
| `.\llenar_marketplace_sesion_abierta.ps1 [-ManualCategory] [-Publish] [-ConfirmPublish]` | Llena el formulario con la sesion abierta (puerto 9222). Sin `-Publish` no avanza; sin `-ConfirmPublish` no publica |
| `.\inspeccionar_categorias_marketplace.ps1` | Lista las categorias que ve el formulario |
| `python marketplace_campaign_runner.py --plan-only` | Expande una campana JSON sin abrir Facebook |
| `python marketplace_listing_builder.py --help` | Genera listados individuales o agrupados |

El publicador (`facebook_marketplace_browser_automator.py`) tiene tres niveles:
sin banderas llena el formulario; `--publish` pulsa `Next`;
`--confirm-publish` publica de verdad. Opciones de entrega: `--meet-public`,
`--door-pickup`, `--door-dropoff`.

Las campanas JSON manuales estan descritas en
[`marketplace_campaign_modes.md`](marketplace_campaign_modes.md).

## Archivos y variables de entorno

Datos locales (ninguno se versiona; ver `.gitignore`):

| Archivo / carpeta | Contenido |
|---|---|
| `marketplace_bot.db` | Cola, intentos, publicaciones, fotos usadas, alertas, pausas, productos manuales |
| `marketplace_accounts.json` | Cuentas, puertos y perfiles de Chrome |
| `marketplace_integrations.json` | SGI y carpetas de Drive |
| `marketplace_dashboard_token.txt` | Llave de la sesion del panel |
| `marketplace_publish_intents/` | Marcas previas a `Publish` |
| `marketplace_runtime_locks/` | Candados por cuenta |
| `marketplace_failures/` | Capturas y diagnostico de fallos |
| `marketplace_scheduler_worker.log` | Log del trabajador |

Variables: `MARKETPLACE_BOT_DB` (ruta alternativa de la base),
`SGI_INTEGRATION_KEY` (llave del SGI sin pasar por el panel), `OPENAI_API_KEY` y
`OPENAI_MODEL` (IA).

## Migrar una instalacion anterior

Antes la carpeta de fotos locales era `imagenes_firupost`, la clave de IA estaba en
`.firupost_openai_key.bin` y el modelo en `FIRUPOST_OPENAI_MODEL`. **Una
instalacion anterior sigue funcionando sin tocar nada**: el bot usa la carpeta y
la clave antiguas si son las unicas que existen, y acepta la variable antigua.

Para pasar a los nombres actuales, con el panel y el trabajador detenidos:

```powershell
python migrar_nombres_antiguos.py --dry-run   # muestra lo que haria
python migrar_nombres_antiguos.py
```

Renombra la carpeta a `imagenes_productos` y la clave a `.openai_key.bin`, y
reescribe en la base de datos y en los JSON de actividad y campana las rutas de
fotos que mencionan el nombre anterior (guarda una copia `.bak-<fecha>` de cada
archivo que cambia). Si ya existen las dos carpetas, no toca nada y avisa para que
las unas a mano. La variable `FIRUPOST_OPENAI_MODEL` se renombra tu mismo a
`OPENAI_MODEL`.

## Codigos de alerta

| Codigo | Significado | Que hacer |
|---|---|---|
| `PUBLISH_OUTCOME_UNKNOWN` | Se pulso `Publish` sin confirmar el resultado | Revisar Marketplace; ver [arriba](#resultados-de-publicacion-inciertos) |
| `IMAGE_REUSED` | La foto ya se uso en esa cuenta | Agregar fotos nuevas o usar otra cuenta |
| `ACCOUNT_RESTRICTED` | Facebook mostro un aviso de limite o restriccion | La cuenta se pausa 72 h; revisarla a mano |
| `SESSION_BLOCKED` | Sesion cerrada, captcha, 2FA o revision | Resolver en Chrome y reintentar |
| `FORM_FIELD` | Un campo del formulario no se pudo llenar | Revisar la captura en `marketplace_failures/` |
| `DATA_INVALID` | Faltan foto, precio, categoria o descripcion validos | Corregir el producto |
| `CONTENT_POLICY` | Facebook rechazo el contenido del anuncio | Cambiar texto o fotos |
| `SAFETY_HOLD` | La proteccion de cuenta freno el anuncio | Nada: se reprograma sin gastar intentos |
| `OUT_OF_STOCK` / `PRICE_ISSUE` / `NO_PHOTOS` | El SGI descarto el producto antes de publicar | Corregir en el SGI o en Drive (sin stock no genera alerta) |
| `SGI_UNAVAILABLE` / `DRIVE_NOT_AUTHORIZED` | No se pudo sincronizar con el SGI o con Drive | Revisar la conexion o volver a autorizar Drive |

Los codigos `SESSION_BLOCKED`, `PUBLISH_OUTCOME_UNKNOWN`, `DATA_INVALID`,
`IMAGE_REUSED`, `ACCOUNT_RESTRICTED` y `CONTENT_POLICY` bloquean el elemento sin
reintentarlo.

Captchas, 2FA y revisiones de cuenta nunca se automatizan: requieren una
persona.
