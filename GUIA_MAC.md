# Bot Marketplace en Mac

El mismo bot corre en Windows y en Mac. Cambian solo los iniciadores y el lugar
donde se guardan las sesiones y las llaves:

| | Windows | Mac |
|---|---|---|
| Abrir el panel | `iniciar_panel_marketplace.ps1` | `iniciar_panel_marketplace.command` |
| Trabajador automatico | Tarea programada `MarketplaceBotScheduler` | launchd (`com.lacomarca.marketplacebot`) |
| Instalar | `pip install` + `instalar_trabajador_marketplace.ps1` | `instalar_mac.command` (hace todo) |
| Perfiles de Chrome | `%LOCALAPPDATA%\MarketplaceBot_...` | `~/Library/Application Support/MarketplaceBot_...` |
| Llave del SGI y de OpenAI | Cifradas con la cuenta de Windows | Llavero de la Mac |

**Una sola computadora a la vez.** Si la PC y la Mac tienen el bot activo con las
mismas cuentas, las dos publican y Facebook ve anuncios duplicados. Antes de
activar la Mac, apaga **Bot activo** en el panel de la PC.

## 1. Requisitos

- Google Chrome instalado en **Aplicaciones**.
- Python 3.10 o mas nuevo, del instalador de
  [python.org](https://www.python.org/downloads/macos/) (3.12 recomendado).
- Conexion a Internet.

## 2. Pasar el bot de la PC a la Mac

1. En el panel de la PC apaga **Bot activo**.
2. En la PC, desde la carpeta del bot, saca una copia segura de la base (se
   puede hacer con el bot corriendo):

   ```powershell
   python -c "import sqlite3; sqlite3.connect('marketplace_bot.db').backup(sqlite3.connect('marketplace_bot_para_mac.db'))"
   ```

3. Copia la carpeta del bot a la Mac (memoria USB, Drive o AirDrop) y dejala en
   tu carpeta de usuario, por ejemplo `~/BotMarketplace`. **No** la pongas en
   Escritorio, Documentos, Descargas ni iCloud Drive: macOS no deja que el
   trabajador automatico lea esas carpetas.
4. En la Mac, dentro de esa carpeta, renombra `marketplace_bot_para_mac.db` a
   `marketplace_bot.db` (reemplaza el que venia en la copia).

Lo que debe ir en la copia:

- `marketplace_bot.db` (la copia del paso 2), `marketplace_accounts.json`,
  `ArticulosGenerados.xlsx` y la carpeta `imagenes_productos`.
- Si usas el SGI y Drive: `marketplace_integrations.json`,
  `credenciales_google.json` y `token_google.json`.

No hace falta copiar logs, `marketplace_runtime_locks/`, archivos `.pid` ni las
llaves `.bin`: las llaves de Windows no se pueden abrir en la Mac.

Las fotos que la base guardo con rutas de Windows (`C:\Users\...\imagenes_productos\...`)
se buscan solas en la carpeta `imagenes_productos` de la Mac. No hay que editar nada.

## 3. Instalar

Abre la app **Terminal** y escribe (cambia la carpeta si usaste otra):

```bash
cd ~/BotMarketplace
```

```bash
bash instalar_mac.command
```

El instalador crea el entorno de Python del bot (`.venv-mac`), instala las
librerias e instala el trabajador automatico. Tarda unos minutos la primera vez.

## 4. Primera vez en la Mac

1. Abre el panel con doble clic en `iniciar_panel_marketplace.command` (o
   `./iniciar_panel_marketplace.command` en la Terminal).
2. En el panel pulsa **Abrir sesion** en cada cuenta e inicia sesion en Facebook.
   Las sesiones de la PC no pasan a la Mac; Facebook puede pedir un codigo.
3. Vuelve a guardar en el panel la llave del SGI y la clave de OpenAI (si las usas).
   La Mac las guarda en el Llavero. Si macOS pregunta si Python puede usar el
   Llavero, responde **Permitir siempre**.
4. Revisa el calendario y enciende **Bot activo**.

## 5. Uso diario

- **Panel:** doble clic en `iniciar_panel_marketplace.command`. Puedes cerrar la
  ventana de la Terminal: el panel sigue corriendo.
- **Trabajador:** arranca solo al iniciar sesion en la Mac y se revisa cada 10
  minutos. Su registro esta en `marketplace_scheduler_worker.log`.
- **Reposo:** con la Mac en reposo el bot no publica (lo mismo que en la PC). En
  una laptop, en Ajustes del Sistema > Bateria, activa la opcion que evita el
  reposo automatico cuando esta conectada a la corriente.
- **Quitar el trabajador automatico:**

  ```bash
  .venv-mac/bin/python instalar_trabajador_mac.py --desinstalar
  ```

## 6. Problemas frecuentes

- **"No se puede abrir porque es de un desarrollador no identificado"** al hacer
  doble clic: clic derecho sobre el archivo > Abrir, o corre el comando con
  `bash` desde la Terminal.
- **"Permission denied"** al hacer doble clic: corre una vez
  `bash instalar_mac.command`, que deja ejecutables los iniciadores.
- **"Operation not permitted"** en el registro: la carpeta del bot esta en
  Escritorio, Documentos, Descargas o iCloud. Muevela a `~/BotMarketplace` y
  corre de nuevo `bash instalar_mac.command`.
- **"No encontre Google Chrome instalado"**: Chrome debe estar en la carpeta
  Aplicaciones.
- **El panel no abre:** revisa `marketplace_dashboard.log` en la carpeta del bot.
