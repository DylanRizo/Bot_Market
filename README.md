# Bot de Facebook Marketplace

Publica anuncios en Facebook Marketplace de forma programada: elige los
productos, arma el calendario de la semana, rellena el formulario en el
navegador y deja constancia de cada intento. Se administra desde un panel web
local.

Pensado para una tienda con catalogo en Google Sheets y WooCommerce, varias
cuentas de Marketplace y la necesidad de publicar sin estar delante.

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
- **Trabajador permanente.** Corre como tarea de Windows: el calendario avanza
  aunque el panel este cerrado, y sobrevive a reinicios.

- **Proteccion de cuenta.** Antes de cada anuncio real revisa limites por cuenta
  (al dia, en 7 dias y separacion minima), aplica un calentamiento a las cuentas
  que recien empiezan a vender (1 anuncio al dia la primera semana, 2 la
  segunda), espera dias antes de repetir un producto en la misma cuenta, separa
  las cuentas entre si y varia titulo y descripcion por cuenta. Si Facebook
  muestra un aviso de limite o restriccion, pausa la cuenta 72 horas; tras
  fallos seguidos la deja descansar 12. Tambien bloquea anuncios con enlaces o
  correos y avisa de telefonos, mayusculas y titulos largos. Lo que frena la
  proteccion se reprograma sin gastar intentos. Se configura en
  `Automatizacion > Reglas > Proteccion de cuenta` (`marketplace_safety.py`).

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

## Puesta en marcha

```powershell
python -m pip install -r requirements_marketplace_bot.txt
copy marketplace_accounts.example.json marketplace_accounts.json
```

Edita `marketplace_accounts.json` con tus cuentas y coloca junto al bot tu
`ArticulosGenerados.xlsx` y la carpeta `imagenes_firupost`. Despues:

```powershell
powershell -ExecutionPolicy Bypass -File .\abrir_sesion_marketplace_cuenta.ps1   # inicia sesion una vez
powershell -ExecutionPolicy Bypass -File .\iniciar_panel_marketplace.ps1         # abre el panel
```

El panel pide una llave que genera al arrancar; el script abre el enlace ya con
ella incluida. Solo escucha en `127.0.0.1`.

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
marketplace_storage.py         base SQLite: cola, intentos, publicaciones, fotos usadas, alertas
marketplace_scheduler.py       reglas del calendario y seleccion de productos
marketplace_scheduler_worker.py  proceso permanente que ejecuta la cola
marketplace_campaign_runner.py   lanza cada publicacion con su candado de cuenta
facebook_marketplace_browser_automator.py  rellena y publica el formulario con Selenium
marketplace_listing_builder.py   titulos, descripciones, agrupacion de variantes
marketplace_catalog.py           lee el inventario y arma las familias de producto
marketplace_ai_descriptions.py   redaccion opcional por IA
marketplace_bot_dashboard.py     servidor del panel
sgi_client.py                    lectura del catalogo del SGI con la llave de integracion
drive_photos.py                  fotos de producto desde Google Drive (solo lectura)
sgi_sync.py                      genera el Excel y las carpetas de fotos desde SGI + Drive
static/                          frontend del panel (html, css, js)
firupost_automator.py            sincroniza Google Sheets + WooCommerce al Excel
legacy/                          generacion anterior, ya no se usa
tests/                           suite de regresion
```

## Pruebas

```powershell
python -m pip install -r requirements_dev.txt
python -m pytest
```

Corren sin red, sin navegador y contra una base temporal, nunca contra la real.
Cubren la cola y los reintentos, el calendario y la rotacion, las reglas que
evitan anuncios duplicados, la redaccion de anuncios y el panel.

## Documentacion

- [`README_firupost_automation.md`](README_firupost_automation.md) — manual de operacion detallado
- [`PLAN_BOT_MARKETPLACE_AUTONOMO.md`](PLAN_BOT_MARKETPLACE_AUTONOMO.md) — diseno por fases
- [`GUIA_SOCIOS_MARKETPLACE_BOT.md`](GUIA_SOCIOS_MARKETPLACE_BOT.md) — guia para instalaciones de terceros
- [`legacy/README.md`](legacy/README.md) — por que existe la carpeta `legacy`

## Aviso

Automatizar Facebook Marketplace puede chocar con las condiciones de uso de
Facebook. Usalo sobre cuentas propias y bajo tu responsabilidad. El proyecto se
publica sin garantia de ningun tipo.

## Licencia

MIT. Ver [LICENSE](LICENSE).
