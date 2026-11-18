# Bot Marketplace La Comarca

Publica anuncios en Facebook Marketplace de forma programada: elige los
productos, arma el calendario de la semana, rellena el formulario en el
navegador y deja constancia de cada intento. Se administra desde un panel web
local.

Pensado para una tienda con varias cuentas de Marketplace y la necesidad de
publicar sin estar delante. El inventario sale de un SGI; las fotos, de Google
Drive y de una carpeta local de respaldo.

## Que hace

- **Calendario semanal.** Se definen dias, horario, limite diario e intervalo, y
  el bot reparte los productos en los huecos disponibles. Los horarios llevan una
  variacion de unos minutos para que el patron no sea siempre identico.
- **Rotacion automatica.** Elige que producto toca segun cuanto lleva sin
  publicarse, no repite dentro del periodo configurado y alterna familias.
- **Sin duplicados.** Antes de pulsar `Publish` deja una marca en disco. Si el
  proceso muere despues, el trabajo queda bloqueado con una alerta en vez de
  reintentarse, porque el anuncio pudo haberse creado.
- **Historial de fotos por cuenta.** Una foto publicada en una cuenta no se
  vuelve a programar ahi; la misma foto si puede usarse en otra.
- **Varias cuentas.** Cada una con su sesion de Chrome, su candado de ejecucion,
  su historial y sus alertas.
- **Descripciones y etiquetas.** Redaccion comercial local, u opcionalmente
  generada por una API compatible con OpenAI.
- **Trabajador permanente.** Corre como tarea de Windows (o con launchd en la
  Mac): el calendario avanza aunque el panel este cerrado, y sobrevive a reinicios.
- **Proteccion de cuenta.** Antes de cada anuncio real aplica limites por
  cuenta (al dia, en 7 dias y separacion minima), calienta las cuentas nuevas,
  separa las cuentas entre si, varia la redaccion por cuenta, bloquea enlaces y
  correos, y pausa la cuenta 72 horas si Facebook muestra una restriccion. Lo que
  frena la proteccion se reprograma sin gastar intentos. Detalle en el
  [manual](MANUAL_OPERACION.md#proteccion-de-cuenta).

Nunca guarda la contrasena de Facebook: reutiliza un perfil de Chrome con la
sesion ya iniciada. Captchas, 2FA y revisiones de cuenta detienen esa cuenta y
piden intervencion humana.

## Niveles de autonomia

El bot se activa por escalones, del mas seguro al mas automatico:

| Nivel | Que hace |
|---|---|
| `Simulacion` | Valida el plan sin abrir Facebook |
| `Prueba sin publicar` | Rellena el formulario real y se detiene antes de publicar |
| `Supervisado` | Igual, pero exige aprobar cada elemento |
| `Calendario aprobado` | Publica unicamente lo aprobado |
| `Autonomo` | Genera, aprueba y publica solo |

Por defecto el bot arranca en `Supervisado` y con `Bot activo` apagado.

## Requisitos

Windows 10 u 11 o una Mac, Google Chrome, Python 3.10 a 3.12 y una cuenta de
Facebook con acceso a Marketplace. Las claves se guardan cifradas con DPAPI en
Windows y en el Llavero en la Mac. Para la Mac mira [`GUIA_MAC.md`](GUIA_MAC.md).

## Puesta en marcha

```powershell
python -m pip install -r requirements_marketplace_bot.txt
copy marketplace_accounts.example.json marketplace_accounts.json
```

Edita `marketplace_accounts.json` con tus cuentas y coloca junto al bot tu
`ArticulosGenerados.xlsx` y la carpeta `imagenes_productos`. Despues:

```powershell
powershell -ExecutionPolicy Bypass -File .\abrir_sesion_marketplace_cuenta.ps1   # inicia sesion una vez
powershell -ExecutionPolicy Bypass -File .\iniciar_panel_marketplace.ps1         # abre el panel
powershell -ExecutionPolicy Bypass -File .\instalar_trabajador_marketplace.ps1   # trabajador permanente (opcional)
```

El panel pide una llave que genera al arrancar; el script abre el enlace ya con
ella incluida (puerto 8794). Solo escucha en `127.0.0.1`.

## Conexion con el SGI y Google Drive (opcional)

En lugar del Excel manual, el bot puede tomar el inventario de un SGI La Comarca
y las fotos de Google Drive:

- publica **solo lo que tiene stock** en el SGI, con su **precio de venta**;
- si un producto tiene precios distintos entre bodegas, precio en revision o sin
  precio, **no lo publica** y lo muestra en el panel;
- busca en toda la carpeta de Drive configurada, con sus subcarpetas, las fotos
  nombradas con el codigo del SGI (`CODIGO-COLOR - descripcion - 001.jpg`); si
  no hay, usa la foto local de respaldo. Pone primero las fotos del producto y
  despues flyers, publicidad e infografias. No usa fotos de inventario ni de
  empaque;
- justo antes de cada anuncio vuelve a comprobar que el producto sigue con stock.

Configuracion:

1. `copy marketplace_integrations.example.json marketplace_integrations.json` y
   pon la direccion del SGI y las carpetas de Drive. Ese archivo no se versiona.
2. En el SGI, **Configuracion → Integraciones**, crea una llave de solo lectura.
   Se muestra una sola vez.
3. En el panel, pestana **Automatizacion → Conexion con el SGI**, pega la llave.
   Se guarda cifrada con DPAPI y no vuelve a mostrarse. Pulsa
   **Autorizar Google Drive** (permiso de solo lectura) y luego
   **Sincronizar ahora**.

Si revocas la llave en el SGI, el trabajador deja de publicar y levanta una
alerta.

## Estructura

```
marketplace_storage.py           base SQLite: cola, intentos, publicaciones, fotos usadas, alertas
marketplace_scheduler.py         reglas del calendario y seleccion de productos
marketplace_scheduler_worker.py  proceso permanente que ejecuta la cola
marketplace_safety.py            proteccion de cuenta: limites, calentamiento, pausas, revision del texto
marketplace_runtime.py           candados por cuenta y utilidades de ejecucion
marketplace_campaign_runner.py   lanza cada publicacion con su candado de cuenta
facebook_marketplace_browser_automator.py  rellena y publica el formulario con Selenium
marketplace_listing_builder.py   titulos, descripciones, agrupacion de variantes
marketplace_catalog.py           lee el inventario y arma las familias de producto
marketplace_custom_products.py   productos manuales fuera del inventario
marketplace_ai_descriptions.py   redaccion opcional por IA
marketplace_bot_dashboard.py     servidor del panel
static/                          frontend del panel (html, css, js)
sgi_client.py                    lectura del catalogo del SGI con la llave de integracion
drive_photos.py                  fotos de producto desde Google Drive (solo lectura)
sgi_sync.py                      genera el Excel y las carpetas de fotos desde SGI + Drive
*.ps1                            iniciadores de Windows (panel, trabajador, sesiones, paquete)
*.command, *.sh                  iniciadores de la Mac (instalar, panel, trabajador)
marketplace_platform.py          lo que cambia entre Windows y Mac (Chrome, procesos, llaves)
instalar_trabajador_mac.py       trabajador permanente en la Mac (launchd)
tests/                           suite de regresion
```

Las columnas del Excel (`Titulo`, `Precio`, `Categoria`, ...) vienen del formato
de FiruPost, pero el bot ya no usa ese programa. Si vienes de una instalacion
anterior (carpeta `imagenes_firupost`), mira
[Migrar una instalacion anterior](MANUAL_OPERACION.md#migrar-una-instalacion-anterior).

## Pruebas

```powershell
python -m pip install -r requirements_dev.txt
python -m pytest
```

Corren sin red, sin navegador y contra una base temporal, nunca contra la real
(unas 150 pruebas). Cubren la cola y los reintentos, el calendario y la rotacion,
la proteccion de cuenta, las reglas que evitan anuncios duplicados, la redaccion
de anuncios, el catalogo, el cliente del SGI, las fotos de Drive y el panel. Una
accion de GitHub (`.github/workflows/tests.yml`) las corre en cada push y pull
request, en Windows y en Mac. Conviene correrlas antes de tocar el publicador o
el planificador.

## Documentacion

- [`MANUAL_OPERACION.md`](MANUAL_OPERACION.md) — uso diario: reglas, proteccion, fotos, alertas, comandos
- [`GUIA_SOCIOS_MARKETPLACE_BOT.md`](GUIA_SOCIOS_MARKETPLACE_BOT.md) — instalacion en computadoras de terceros
- [`GUIA_MAC.md`](GUIA_MAC.md) — instalar el bot en una Mac o pasarlo de la PC a la Mac
- [`PLAN_BOT_MARKETPLACE_AUTONOMO.md`](PLAN_BOT_MARKETPLACE_AUTONOMO.md) — diseno por fases y estado
- [`marketplace_campaign_modes.md`](marketplace_campaign_modes.md) — campanas JSON manuales

## Aviso

Automatizar Facebook Marketplace puede chocar con las condiciones de uso de
Facebook. Usalo sobre cuentas propias y bajo tu responsabilidad. El proyecto se
publica sin garantia de ningun tipo.

## Monitor del SGI

El trabajador local puede enviar al SGI el estado de las cuentas, los interesados,
las recomendaciones y el calendario. La vista se encuentra en **Bot Marketplace**.
El pulso se intenta cada 30 segundos entre ciclos del trabajador, incluso cuando
las publicaciones están pausadas.

En **Configuración → Integraciones** del SGI, crea una llave de sincronización del
bot y elige un identificador estable para esta instalación. Copia ese identificador
en `sgi.monitor.instance_key` de `marketplace_integrations.json`, define un
`instance_name` y activa `enabled`. El archivo de ejemplo muestra la estructura.

Guarda la llave desde esta carpeta con la misma cuenta que ejecuta el trabajador.
Windows usa DPAPI y Mac usa el Llavero, con una entrada propia del monitor.

```powershell
python -c "from getpass import getpass; from sgi_monitor import save_monitor_key; save_monitor_key(getpass('Llave del monitor: '))"
```

Reinicia el trabajador y comprueba la hora del último pulso en el SGI. Al vencer
la llave, crea otra para el mismo identificador y guárdala con el mismo comando.
La variable `SGI_MONITOR_KEY` permite proporcionar la llave al proceso.

El control remoto requiere `MARKETPLACE_BOT_CONTROL_ENABLED=true` en el SGI y
`sgi.monitor.control_enabled=true` en la configuración local. Su activación sigue
el gate operacional del SGI y exige que la cuenta tenga `integrations.manage`.
Las opciones desactivadas mantienen el modo de consulta.

El cliente guarda el ID y el resultado de cada orden en los ajustes de su base
local antes de confirmar el resultado al SGI. Un reenvío conserva el resultado
guardado. Una orden vencida se rechaza. Si el proceso se interrumpe durante una
acción, la siguiente conexión informa que requiere verificar el estado local
antes de solicitarla otra vez. Los resultados se conservan hasta que el SGI
confirme su recepción.

Las acciones sobre anuncios y las solicitudes de revisión se añaden a la cola
local y las ejecuta el trabajador cuando está activo. El resultado de la orden
confirma esa solicitud, y el monitor permite seguir su ejecución posterior.

El pulso corre en un hilo propio cada 30 segundos, también mientras el
trabajador publica. Ocupado, solo informa qué publica y entrega resultados; las
órdenes nuevas se ejecutan apenas termina. Desde el SGI se puede cambiar el
horario de publicación (días, horas, máximo diario y separación), mover una
publicación pendiente o publicarla ya, siempre dentro del horario y los límites
de seguridad.

Los pulsos incluyen una proyección comercial acotada. Los mensajes y datos de
contacto de compradores permanecen en el equipo local.

## Licencia

MIT. Ver [LICENSE](LICENSE).
