# Generacion anterior: automatizacion de la aplicacion FiruPost

Lo que hay en esta carpeta **no es el bot actual**. Automatiza la aplicacion de
escritorio FiruPost controlando su interfaz con `uiautomation`: se abria el
programa, se cargaba el Excel y las fotos, y era el propio FiruPost quien
publicaba en Marketplace.

El bot actual no depende de FiruPost. Publica directamente en el navegador con
`facebook_marketplace_browser_automator.py`, que nacio como plan B y acabo
siendo el motor: es mas rapido, no exige tener el programa instalado y permite
verificar el resultado de cada publicacion.

Se conserva porque sigue funcionando y sirve de referencia, pero no recibe
mantenimiento ni tiene pruebas.

| Archivo | Que hacia |
|---|---|
| `firupost_ui_automator.py` | Manejaba la interfaz de FiruPost: licencia, formulario, carga de Excel e imagenes |
| `firupost_launch_assistant.py` | Preparaba el lote y abria FiruPost |
| `firupost_make_test_batch.py` | Generaba lotes pequenos de prueba |
| `publicar_prueba_1_producto.ps1` | Publicaba un producto con FiruPost |
| `publicar_prueba_sesion_abierta.ps1` | Igual, reutilizando una sesion ya iniciada |
| `abrir_sesion_facebook_firupost.ps1` | Abria Chrome con el perfil que usaba FiruPost |

`firupost_automator.py` **no** esta aqui: sigue en la raiz porque es la
sincronizacion de inventario (Google Sheets + WooCommerce a
`ArticulosGenerados.xlsx`) de la que depende el bot actual.

## Como se usaba (referencia)

Esto describe el flujo anterior tal como funcionaba; no se prueba desde que el
bot actual lo reemplazo. Los comandos se ejecutan desde esta carpeta y necesitan
`uiautomation` y FiruPost instalado en `C:\FiruPost`.

1. `abrir_sesion_facebook_firupost.ps1` abre Chrome con el perfil
   `%LOCALAPPDATA%\BotInvisibleChrome`; se inicia sesion en Facebook una vez.
2. `python firupost_launch_assistant.py` regenera `ArticulosGenerados.xlsx` y
   `prompt_firupost_ia.txt`, escribe `firupost_session_config.json` y abre FiruPost.
3. `python firupost_ui_automator.py --mode individual --reuse-session --test-batch
   --test-layout direct --test-limit 1 --test-condition New` carga un lote de
   prueba de un producto. Con `--start` pulsa `Iniciar`. Opciones de entrega:
   `--meet-public`, `--door-pickup`, `--door-delivery`, `--publish-groups`,
   `--publish-without-description`.
4. `publicar_prueba_sesion_abierta.ps1` y `publicar_prueba_1_producto.ps1`
   envuelven el paso anterior.

Requisitos de FiruPost que explican varias decisiones del bot actual: Facebook en
`English (US)`, columnas `Titulo`, `Precio`, `Categoria`, `Condicion`,
`Descripcion`, `Etiqueta`, `Sku`, `Ubicacion`, `CarpetaImg`, `NombreImg`, y
`Cuentas_FB.xlsx` con `email`, `password`, `Nombre_Cuentas` para el modo de
cuentas multiples (el bot actual no guarda contrasenas). FiruPost nunca resolvio
de forma fiable la categoria `Apparel` en el selector de creacion, y era una de
las razones del cambio al publicador directo.

Documentacion oficial de FiruPost: https://firupost.netlify.app/docs

`prompt_firupost_ia.txt`, en la raiz, es el prompt para el modulo `Responder
Mensajes IA` de FiruPost (placeholders `{Titulo}`, `{Precio}`, `{Condicion}`,
`{Descripcion}`, `{Stock}`, `{Ubicacion}`, `{Sku}`). Lo regenera
`firupost_automator.py`; el bot actual no lo usa.
