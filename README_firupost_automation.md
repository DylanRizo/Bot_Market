# FiruPost automation workflow

Este directorio contiene el bot que prepara el paquete listo para FiruPost.

La hoja de ruta para convertirlo en un bot semanal autonomo esta en
`PLAN_BOT_MARKETPLACE_AUTONOMO.md`.

## Bot semanal autonomo

Abre el panel simplificado con:

```powershell
powershell -ExecutionPolicy Bypass -File "C:\ruta\al\bot\iniciar_panel_marketplace.ps1"
```

En la pestana `Automatizacion` configura dias, horario, limite diario,
intervalo, cuenta y productos. `Generar semana` crea el calendario sin
publicar. Los niveles funcionan asi:

- `Simulacion`: valida el plan sin abrir Facebook.
- `Prueba sin publicar`: llena el formulario y se detiene.
- `Supervisado`: requiere aprobar los elementos y conserva la publicacion en prueba.
- `Calendario aprobado`: publica unicamente los elementos aprobados.
- `Autonomo`: aprueba y ejecuta automaticamente el calendario.

El trabajador se inicia con Windows mediante la tarea
`MarketplaceBotScheduler`. El interruptor `Bot activo` permanece apagado por
defecto. Captchas, 2FA, revisiones y resultados de publicacion inciertos
bloquean la cuenta y crean una alerta; no se reintentan a ciegas.

La base persistente es `marketplace_bot.db`. Los JSON anteriores se conservan
para compatibilidad y diagnostico.

### Llave del panel

El panel controla el bot entero, asi que pide una llave. Al arrancar genera un
token nuevo, lo guarda en `marketplace_dashboard_token.txt` y lo incluye en el
enlace que abre `iniciar_panel_marketplace.ps1`. Sin ese enlace el panel
responde `No autorizado`; el token vive solo mientras el proceso este abierto.

Si abres el panel a mano, usa la direccion completa con `?token=...` que imprime
el script. El panel solo escucha en esta computadora: pedir `--host` distinto de
`127.0.0.1` falla salvo que agregues `--allow-remote` a proposito.

### Resultados de publicacion inciertos

Justo antes de pulsar `Publish`, el publicador deja un archivo en
`marketplace_publish_intents/`. Si despues de eso el proceso se corta por
timeout, por un cierre inesperado o por un reinicio de Windows, el trabajador ve
ese archivo y marca el trabajo como `blocked` con la alerta
`PUBLISH_OUTCOME_UNKNOWN`. **No lo reintenta**, porque el anuncio pudo haberse
creado y un reintento lo duplicaria.

Para resolverlo: revisa Marketplace. Si el anuncio no existe, pulsa `Reintentar`
en el calendario; el panel borra la marca y vuelve a encolar el trabajo. Si el
anuncio si existe, cancela el elemento.

Cuando una publicacion si se confirma, el bot guarda la URL del anuncio en
`listing_url`, visible en el historial.

### Horarios con variacion

Publicar todos los dias a las 09:00, 12:00 y 15:00 clavadas es un patron
mecanico. `Automatizacion > Variacion del horario (min)` mueve cada publicacion
unos minutos al azar (7 por defecto, maximo 30). La variacion es estable: volver
a generar la misma semana da los mismos horarios, y nunca llega a solapar dos
espacios seguidos.

### Reintentos

Un fallo temporal no se reintenta siempre a la misma distancia: cada intento
duplica la espera desde `retry_delay_minutes`, con tope de 6 horas y una
variacion de mas o menos 20%. Al agotar `max_attempts` el trabajo queda
`blocked`.

### Fotos por cuenta

En `Automatizacion > Fotos definidas por producto y cuenta` se muestran las
miniaturas que usara cada producto en cada cuenta activa. Marca o desmarca las
fotos y pulsa `Guardar reglas`. La seleccion se aplica al generar el siguiente
calendario; los trabajos que ya estaban programados conservan su configuracion.

El bot calcula una huella SHA-256 del contenido de cada foto. Una foto publicada
en una cuenta no vuelve a programarse para esa misma cuenta. El mismo archivo si
puede utilizarse en otra cuenta, porque el historial se mantiene por cuenta.

Cuando todas las fotos de un producto ya fueron utilizadas en una cuenta, el
calendario omite ese producto para esa cuenta. Si un trabajo llega igualmente al
publicador, queda `blocked` con la alerta `IMAGE_REUSED`. Para volver a publicar
en esa cuenta hay que agregar fotos reales nuevas desde la revision o el
producto manual.

El historial comienza con las publicaciones realizadas por el nuevo trabajador;
las publicaciones antiguas que no guardaron sus rutas de fotos no pueden
reconstruirse automaticamente.

### Varias cuentas

En `Automatizacion > Cuentas activas` selecciona una o varias cuentas y elige:

- `Rotar entre cuentas`: distribuye los anuncios alternadamente.
- `Publicar en todas`: crea el mismo anuncio para cada cuenta seleccionada. Los
  horarios de las cuentas se separan siete minutos para evitar ejecuciones
  simultaneas.

Cada cuenta conserva su propia sesion de Chrome, candado de ejecucion, historial
de fotos, reintentos y alertas.

Para una publicacion manual, abre `Nueva publicacion` y elige la cuenta en el
selector visible `Cuenta de Marketplace` antes de seleccionar los productos.

### Productos fuera del inventario

En `Automatizacion > Agregar fuera del inventario` puedes indicar nombre,
precio, categoria, condicion, ubicacion, etiquetas, descripcion y hasta diez
fotos. Si la descripcion queda vacia, se crea una redaccion comercial local.

El producto se guarda en `marketplace_custom_products`, genera su propio Excel y
puede incluirse en la rotacion semanal sin modificar `ArticulosGenerados.xlsx`.

## Archivos principales

- `firupost_automator.py`: sincroniza inventario, genera descripciones, valida imagenes y crea el Excel compatible con FiruPost.
- `legacy/firupost_launch_assistant.py`: ejecuta el preparador, crea un archivo de configuracion de sesion y abre FiruPost.
- `legacy/firupost_ui_automator.py`: abre/adjunta FiruPost, espera la actualizacion, acepta licencia, entra a Publicar, carga imagenes/Excel y deja el formulario listo.
- `legacy/abrir_sesion_facebook_firupost.ps1`: abre Chrome con el perfil que usa FiruPost para iniciar sesion una sola vez.
- `legacy/publicar_prueba_sesion_abierta.ps1`: intenta publicar 1 producto usando una sesion ya abierta, sin pedir contrasena.
- `facebook_marketplace_browser_automator.py`: plan B para llenar el formulario de Marketplace directo en navegador con la sesion abierta.
- `inspeccionar_categorias_marketplace.ps1`: abre/llena un producto y muestra categorias visibles para resolver el bloqueo de `Category`.
- `llenar_marketplace_sesion_abierta.ps1`: llena el formulario directo de Marketplace sin publicar por defecto.
- `firupost_ai_description_openai_compatible.py`: wrapper de IA por API compatible con OpenAI para generar descripciones.
- `ArticulosGenerados.xlsx`: archivo que debes cargar en FiruPost como `Archivo Articulo`.
- `imagenes_firupost`: carpeta que debes cargar en FiruPost como `Carpetas Imagenes`.
- `firupost_validation_report.xlsx`: reporte de productos publicables y productos con problemas.
- `prompt_firupost_ia.txt`: prompt para el modulo `Responder Mensajes IA`.
- `firupost_session_config.json`: resumen de rutas y opciones recomendadas para la sesion.

## Instalacion desde el repositorio

La configuracion con tus cuentas reales no viaja en el repositorio. Al clonar:

```powershell
python -m pip install -r requirements_marketplace_bot.txt
copy marketplace_accounts.example.json marketplace_accounts.json
```

Edita `marketplace_accounts.json` con tus cuentas: una clave por cuenta, su
`display_name`, un `debugger_address` distinto para cada una (9222, 9223, ...) y
la carpeta de perfil de Chrome donde quedara iniciada la sesion de Facebook.

Despues coloca `ArticulosGenerados.xlsx` y la carpeta `imagenes_firupost` junto
a los archivos del bot. Ni el inventario, ni las fotos, ni la base de datos, ni
los tokens estan en el repositorio: son datos tuyos.

## Pruebas

El proyecto tiene una suite de regresion que corre sin red, sin Chrome y sin
tocar la base real (cada prueba usa una base temporal):

```powershell
python -m pip install -r requirements_dev.txt
python -m pytest
```

Cubre la cola y los reintentos (`tests/test_storage.py`), el calendario y la
rotacion (`tests/test_scheduler.py`), las reglas que evitan anuncios duplicados
(`tests/test_worker.py`) y la redaccion de anuncios
(`tests/test_listing_builder.py`). Conviene correrla antes de tocar el
publicador o el planificador.

El repositorio ignora la base de datos, los tokens, las claves de IA, los logs y
las capturas: revisa `.gitignore` antes de agregar archivos nuevos.

## Regenerar todo desde Google Sheets + WooCommerce

```powershell
python "C:\ruta\al\bot\firupost_automator.py" --source sync
```

Salida esperada actual:

- 63 productos publicables.
- 53 SKUs en reporte con `SKU no encontrado en WooCommerce`.
- 0 imagenes rotas en el Excel final.
- 0 SKUs duplicados en el Excel final.

## Descripciones generadas con IA

El preparador puede llamar un comando externo para crear la columna `Descripcion`.
Ese comando recibe un JSON por `stdin` con los datos del producto y debe devolver
solo la descripcion final por `stdout`.

La forma mas estable en PowerShell es configurarlo por variable de entorno:

```powershell
$env:FIRUPOST_AI_DESCRIPTION_COMMAND='python "C:\ruta\a\mi_generador_ia.py"'
python "C:\ruta\al\bot\firupost_automator.py" --source sync
```

Tambien se puede pasar directamente con `--ai-description-command`, pero la
variable de entorno evita problemas de comillas cuando la ruta contiene espacios.
Si el comando de IA falla, el bot usa la descripcion local de respaldo para no
detener la preparacion del lote.

Wrapper listo para API compatible con OpenAI:

```powershell
$env:FIRUPOST_AI_API_KEY="tu_api_key"
$env:FIRUPOST_AI_MODEL="modelo_que_quieres_usar"
powershell -ExecutionPolicy Bypass -File "C:\ruta\al\bot\regenerar_excel_con_ia.ps1"
```

Tambien puedes usar `OPENAI_API_KEY` en vez de `FIRUPOST_AI_API_KEY`. Si usas
otro proveedor compatible, define:

```powershell
$env:FIRUPOST_AI_BASE_URL="https://tu-proveedor.example/v1"
```

La llave y el modelo no se guardan en archivos; se leen desde variables de entorno.

## Sesion abierta sin volver a loguear

FiruPost usa un perfil de Chrome separado:

```text
%LOCALAPPDATA%\BotInvisibleChrome
```

Abre ese perfil y deja Facebook/Marketplace logueado:

```powershell
powershell -ExecutionPolicy Bypass -File "C:\ruta\al\bot\abrir_sesion_facebook_firupost.ps1"
```

Cuando ya este logueado, usa el modo `--reuse-session`. Este modo no requiere
contrasena y limpia el campo password del formulario de FiruPost.

Prueba de 1 producto con sesion abierta y opciones de entrega marcadas:

```powershell
powershell -ExecutionPolicy Bypass -File "C:\ruta\al\bot\publicar_prueba_sesion_abierta.ps1"
```

Opcionalmente puedes pasar el correo solo como etiqueta de la sesion, sin password:

```powershell
powershell -ExecutionPolicy Bypass -File "C:\ruta\al\bot\publicar_prueba_sesion_abierta.ps1" -Email "correo_o_numero" -AccountName "Cuenta principal"
```

## Plan B: llenar Marketplace directo en navegador

Si FiruPost vuelve a fallar con `Category` o `Condition`, usa el llenador directo.
Primero abre la sesion con puerto local:

```powershell
powershell -ExecutionPolicy Bypass -File "C:\ruta\al\bot\abrir_sesion_facebook_firupost.ps1"
```

Luego inspecciona que categorias ve el formulario:

```powershell
powershell -ExecutionPolicy Bypass -File "C:\ruta\al\bot\inspeccionar_categorias_marketplace.ps1"
```

Para llenar imagen, titulo, precio, descripcion y condicion sin publicar:

```powershell
powershell -ExecutionPolicy Bypass -File "C:\ruta\al\bot\llenar_marketplace_sesion_abierta.ps1" -ManualCategory
```

Ese modo se detiene si no encuentra la categoria. Con `-ManualCategory`, te deja
elegir la categoria manualmente y espera a que `Next` quede habilitado. Para
avanzar al siguiente paso hay que agregar `-Publish` explicitamente.

## Abrir asistente de sesion

```powershell
python "C:\ruta\al\bot\firupost_launch_assistant.py"
```

Este comando:

1. Regenera `ArticulosGenerados.xlsx`.
2. Regenera `prompt_firupost_ia.txt`.
3. Escribe `firupost_session_config.json`.
4. Intenta abrir `C:\FiruPost\FiruPost.exe`.
5. Copia al portapapeles la ruta del Excel final.

## Configurar FiruPost automaticamente

Lote de prueba recomendado, 1 producto, sin publicar:

```powershell
python "C:\ruta\al\bot\firupost_ui_automator.py" --mode individual --test-batch --test-layout direct --test-limit 1 --test-condition New
```

Ese comando carga:

- Excel: `C:\ruta\al\bot\firupost_test_batch\ArticulosGenerados_TEST.xlsx`
- Carpeta de imagenes: `C:\ruta\al\bot\firupost_test_batch\imagenes`

El lote usa `CarpetaImg` vacio y `NombreImg` con extension real, por ejemplo
`TSBL-S_foto_1.jpg`. FiruPost busca la ruta como `Carpetas Imagenes\NombreImg`.

Preflight sin abrir FiruPost:

```powershell
python "C:\ruta\al\bot\firupost_ui_automator.py" --mode individual --test-batch --test-layout direct --test-limit 1 --test-condition New --preflight-only
```

Modo individual, sin presionar publicar:

```powershell
python "C:\ruta\al\bot\firupost_ui_automator.py" --mode individual
```

Con credenciales desde variables de entorno:

```powershell
$env:FIRUPOST_FB_EMAIL="correo_o_numero"
$env:FIRUPOST_FB_PASSWORD="tu_contrasena"
$env:FIRUPOST_FB_ACCOUNT_NAME="Perfil negocio"
python "C:\ruta\al\bot\firupost_ui_automator.py" --mode individual
```

Para pedir la contrasena por consola sin guardarla:

```powershell
python "C:\ruta\al\bot\firupost_ui_automator.py" --mode individual --email "correo_o_numero" --account-name "Perfil negocio" --ask-password
```

Para que tambien presione `Iniciar`, hay que pasar `--start` explicitamente:

```powershell
powershell -ExecutionPolicy Bypass -File "C:\ruta\al\bot\publicar_prueba_1_producto.ps1"
```

Ese script pide:

- Correo o numero de Facebook.
- Nombre de cuenta para FiruPost.
- Contrasena por consola, sin guardarla.

Internamente ejecuta FiruPost en modo individual con `--test-batch --test-layout direct --test-limit 1 --test-condition New --start`.
Usa `--start` primero con lote de prueba. Si Facebook pide captcha, 2FA o revision manual, debe resolverse manualmente.

Para usar sesion abierta sin contrasena, el comando base es:

```powershell
python "C:\ruta\al\bot\firupost_ui_automator.py" --mode individual --reuse-session --account-name "Cuenta principal" --test-batch --test-layout direct --test-limit 1 --test-condition New --meet-public --door-pickup --door-delivery --start
```

Opciones de entrega/envio disponibles:

- `--meet-public`: marca `Encuentro en un Lugar Publico`.
- `--door-pickup`: marca `Retiro en la Puerta`.
- `--door-delivery`: marca `Entrega en la puerta`.
- `--publish-groups`: marca `Publicar en Grupos de Facebook`.
- `--publish-without-description`: marca `Publicar sin Descripción`.

## Cargar en FiruPost

Antes de publicar, pon Facebook en idioma `English (US)`. La documentacion de
FiruPost indica que los modulos funcionan correctamente con Facebook en ingles.

1. Abre FiruPost.
2. Entra a `Publicar`.
3. Elige `Publicar En Articulo`.
4. Selecciona `Modo Individual` o `Modo Cuentas Multiples`.
5. En `Carpetas Imagenes`, selecciona:

```text
C:\ruta\al\bot\imagenes_firupost
```

6. En `Archivo Articulo`, selecciona:

```text
C:\ruta\al\bot\ArticulosGenerados.xlsx
```

7. En precio, usa `Precio Original`.
8. En ubicacion, usa `Managua, Nicaragua` o la zona real del negocio.
9. Haz una prueba con pocos productos antes de publicar el lote completo.

## Prompt para responder mensajes con IA

En el modulo `Responder Mensajes IA`, carga el mismo Excel y pega el contenido de:

```text
C:\ruta\al\bot\prompt_firupost_ia.txt
```

El prompt usa placeholders compatibles con FiruPost:

- `{Titulo}`
- `{Precio}`
- `{Condicion}`
- `{Descripcion}`
- `{Stock}`
- `{Ubicacion}`
- `{Sku}`

Ese modulo de FiruPost permite elegir motor IA como Meta AI o Gemini para
responder mensajes. Esto es diferente de la IA previa a la publicacion: nuestro
preparador usa IA para llenar la descripcion del articulo antes de cargar el
Excel; FiruPost usa su modulo IA para contestar chats de compradores despues.

## Referencias de la documentacion oficial

- FiruPost requiere Facebook en `English (US)`.
- El modulo `Publicar` pide credenciales, `Carpetas Imagenes`, `Archivo Articulo`,
  opcion de precio y ubicacion.
- `ArticulosGenerados.xlsx` debe conservar columnas como `Titulo`, `Precio`,
  `Categoria`, `Condicion`, `Descripcion`, `Etiqueta`, `Sku`, `Ubicacion`,
  `CarpetaImg` y `NombreImg`.
- Para modo multiple, `Cuentas_FB.xlsx` usa exactamente `email`, `password` y
  `Nombre_Cuentas`.

Fuente revisada: https://firupost.netlify.app/docs

## Pendientes para automatizacion completa de GUI

La preparacion de inventario y la configuracion de FiruPost ya estan automatizadas hasta dejar el formulario listo.

Estado verificado:

1. FiruPost actualizo de `2.0.1` a `2.1.5` al primer arranque y tardo varios minutos.
2. El Excel completo tiene 63 productos; todas las referencias `CarpetaImg` + `NombreImg` existen y las imagenes abren correctamente.
3. El lote de prueba de 1 producto usa `CarpetaImg` vacio y `NombreImg=TSBL-S_foto_1.jpg`, que resolvio el error `Imagenes Faltantes`.
4. El script pudo aceptar licencia, entrar a `Publicar > Publicar En Articulo`, elegir modo individual, cargar el lote de prueba, marcar `Precio Original`, escribir `Managua, Nicaragua` y abrir Facebook Marketplace.
5. Facebook mostro reCAPTCHA; debe resolverse manualmente.
6. Tras resolver login/captcha, FiruPost lleno imagen, titulo, precio y condicion `New`.
7. Pendiente: elegir una categoria exacta que Facebook acepte en el formulario. `Apparel` aparece en Browse Marketplace, pero FiruPost no la encuentra dentro del selector de creacion cuando el formulario queda en la lista de `Home & Garden`.
8. `C:\FiruPost\Cuentas_FB.xlsx` existe pero esta vacio; para modo multiple hay que llenarlo con `email`, `password`, `Nombre_Cuentas`.

No se debe automatizar captcha, 2FA ni saltar protecciones de Facebook/Windows. El flujo debe arrancar despues de un login normal y autorizado.
