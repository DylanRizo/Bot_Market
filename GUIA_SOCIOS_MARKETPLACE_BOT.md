# Guia para socios: Marketplace Bot

## 1. Modelo recomendado: una instalacion privada por socio

Cada socio debe instalar su propia copia del bot en su computadora. Al iniciar
por primera vez, cada copia crea localmente su propia base de datos y mantiene:

- Su cuenta personal de Facebook Marketplace.
- Su calendario y reglas de publicacion.
- Su historial de publicaciones, errores y fotos utilizadas.
- Sus productos manuales y configuracion de IA.
- Su perfil privado de Chrome con la sesion iniciada.

Las copias no se conectan entre si y no comparten credenciales. Pueden partir
del mismo catalogo comercial, pero cada socio decide que productos, fotos,
precios y horarios usara en su cuenta.

No coloques `marketplace_bot.db` ni las carpetas de perfil de Chrome en Drive,
OneDrive, Dropbox, una unidad de red o una carpeta compartida. Cada instalacion
debe conservar esos datos exclusivamente en el disco local de su propietario.

El panel escucha solo en `127.0.0.1`, por lo que no queda expuesto directamente
a Internet ni a otros equipos de la red. Ademas pide una llave: al arrancar
genera un token, lo guarda en `marketplace_dashboard_token.txt` y lo agrega al
enlace que abre el iniciador. Si abres `http://127.0.0.1:8794/` sin esa llave, el
panel responde `No autorizado`. No compartas ese archivo ni el enlace completo.

## 2. Que se puede compartir

Usa el script `preparar_paquete_socios.ps1`. El paquete limpio contiene el bot,
los iniciadores, esta guia y un archivo de cuentas de ejemplo.

No incluye:

- Contrasenas ni sesiones de Facebook.
- Clave de OpenAI.
- Tokens o credenciales de Google.
- `marketplace_bot.db` ni el historial de tu instalacion.
- Productos manuales, fotos temporales, capturas, logs o archivos de diagnostico.

El mismo ZIP limpio puede enviarse a todos los socios. Cada copia comienza sin
calendario, sin historial y sin sesiones. Los datos que cada socio cree despues
de instalarlo permanecen solamente en su computadora.

Para crear un ZIP sin inventario:

```powershell
powershell -ExecutionPolicy Bypass -File ".\preparar_paquete_socios.ps1"
```

Para incluir `ArticulosGenerados.xlsx` y `imagenes_firupost`:

```powershell
powershell -ExecutionPolicy Bypass -File ".\preparar_paquete_socios.ps1" -IncludeCatalog
```

El ZIP se crea de forma predeterminada en el Escritorio. Revisa su contenido
antes de enviarlo por Drive, memoria USB o el medio privado que utilice el
negocio.

## 3. Requisitos de otra computadora

- Windows 10 u 11.
- Google Chrome instalado.
- Python 3.10, 3.11 o 3.12 disponible como `python.exe`.
- Una cuenta de Facebook con acceso a Marketplace.
- Conexion a Internet.
- Una copia legitima del inventario y las fotos que ese socio deba publicar.

La IA es opcional. Sin clave de OpenAI el bot utiliza redaccion comercial local.

## 4. Instalacion en otra computadora

1. Extrae el ZIP en una carpeta permanente, por ejemplo
   `C:\MarketplaceBot`. No lo ejecutes desde Descargas ni desde dentro del ZIP.
2. Abre PowerShell dentro de esa carpeta.
3. Instala las dependencias:

```powershell
python -m pip install -r ".\requirements_marketplace_bot.txt"
```

4. Inicia el panel:

```powershell
powershell -ExecutionPolicy Bypass -File ".\iniciar_panel_marketplace.ps1"
```

5. El navegador abrira el panel en `http://127.0.0.1:8794/` con la llave de la
   sesion incluida en el enlace. Si cierras la pestana, vuelve a ejecutar el
   iniciador en lugar de escribir la direccion a mano.

Si el paquete no incluyo catalogo, copia `ArticulosGenerados.xlsx` y la carpeta
`imagenes_firupost` junto a los archivos del bot antes de generar un calendario.

## 5. Configurar la cuenta privada del socio

1. Abre la pestana `Cuentas` en su propia computadora.
2. Cambia `cuenta1` por un identificador corto, por ejemplo `maria`.
3. Escribe el nombre visible del socio.
4. Para una sola cuenta puede conservar `127.0.0.1:9222`.
5. Usa una carpeta local de Chrome exclusiva, por ejemplo
   `%LOCALAPPDATA%\MarketplaceBot_maria`.
6. Pulsa `Guardar`.
7. En `Nueva publicacion`, selecciona la cuenta y pulsa `Abrir sesion` si aparece.
8. El socio inicia sesion personalmente en Facebook y completa cualquier 2FA.
9. Deja abierta esa ventana de Chrome cuando el bot vaya a trabajar.

El socio debe escribir personalmente su correo, contrasena y 2FA en Facebook.
Nunca debe enviarlos al administrador del bot ni escribirlos dentro del panel.
El bot reutiliza la sesion local de Chrome y no guarda la contrasena.

Si un socio desea manejar mas de una cuenta en su propia computadora, cada una
debe usar un puerto y una carpeta de Chrome diferentes.

## 6. Crear una publicacion manual

1. Abre `Nueva publicacion`.
2. Selecciona arriba la `Cuenta de Marketplace`.
3. Marca uno o varios productos.
4. Revisa las fotos, su orden, el precio y las etiquetas.
5. Elige si las variantes se agrupan o se publican por color, talla o unidad.
6. Configura la primera hora y la separacion entre anuncios.
7. Usa `Probar primero` para llenar el formulario sin publicar.
8. Revisa Facebook y, solo cuando todo este correcto, usa `Publicar campaña`.

## 7. Configurar la semana automatica

1. Abre `Automatizacion` y deja `Bot activo` apagado mientras configuras.
2. Selecciona el modo. Para comenzar usa `Supervisado`.
3. Elige `Rotar entre cuentas` o `Publicar en todas`.
4. Marca las cuentas, dias, horario, maximo diario e intervalo.
5. Selecciona los productos del catalogo.
6. En `Fotos definidas por producto y cuenta`, marca las fotos permitidas para
   cada cuenta. Puede usarse la misma foto en cuentas diferentes.
7. Pulsa `Guardar reglas`.
8. Pulsa `Generar semana` y revisa el calendario.
9. Aprueba solo los trabajos correctos.
10. Activa el bot cuando todas las sesiones esten abiertas y el calendario este
    revisado.

Cambiar las fotos guardadas afecta al siguiente calendario generado. Los
trabajos que ya estaban programados conservan sus fotos anteriores.

## 8. Productos que no estan en el inventario

En `Automatizacion > Agregar fuera del inventario` indica nombre, precio,
categoria, condicion, ubicacion, etiquetas, descripcion y entre una y diez
fotos. Puedes incluirlo en la rotacion semanal sin modificar el Excel principal.

## 9. Instalar el trabajador semanal

En la computadora de cada socio, abre PowerShell y ejecuta:

```powershell
powershell -ExecutionPolicy Bypass -File ".\instalar_trabajador_marketplace.ps1"
```

Esto crea la tarea de Windows `MarketplaceBotScheduler`. La tarea inicia el
trabajador al entrar a Windows, pero no publica mientras `Bot activo` este
apagado.

## 10. Regla de fotos

El bot identifica cada archivo por su contenido:

- Bloquea una foto ya publicada en la misma cuenta.
- Permite la misma foto en otra cuenta.
- Agregar la misma foto con otro nombre no la convierte en una foto nueva.
- Para repetir un producto en una cuenta deben existir fotos reales nuevas.

## 11. Revision diaria

Antes de dejarlo trabajando:

- Confirma que Windows tenga Internet y no vaya a suspenderse.
- Comprueba que cada ventana de Chrome siga conectada a la cuenta correcta.
- Revisa `Proximas publicaciones` y `Alertas`.
- Verifica precios, fotos y disponibilidad comercial.
- Mantiene `Supervisado` hasta que cada cuenta complete varias pruebas exitosas.

## 12. Problemas frecuentes

`El panel no abre`: confirma que Python este instalado y vuelve a ejecutar
`iniciar_panel_marketplace.ps1`.

`No autorizado`: abriste el panel sin la llave de la sesion. Cierra la pestana y
vuelve a ejecutar `iniciar_panel_marketplace.ps1`, que genera el enlace correcto.

`PUBLISH_OUTCOME_UNKNOWN`: el bot pulso `Publish` pero no pudo confirmar el
resultado, casi siempre porque Facebook se colgo o el equipo se reinicio. **Abre
Marketplace y revisa si el anuncio existe.** Si no existe, pulsa `Reintentar` en
el calendario. Si ya existe, cancela el elemento. El bot no reintenta solo a
proposito: seria la forma mas facil de terminar con el mismo anuncio publicado
dos veces.

`Sesion cerrada`: selecciona la cuenta, abre su sesion y vuelve a iniciar sesion
en la ventana de Chrome correspondiente.

`IMAGE_REUSED`: esa foto ya fue registrada para la cuenta; selecciona fotos
reales nuevas o utiliza otra cuenta.

`No hay productos`: revisa que el Excel y `imagenes_firupost` esten en la
carpeta del bot y que las rutas del inventario correspondan a sus fotos.

`Facebook pide captcha, 2FA o revision`: el socio debe resolverlo manualmente.
No sigas reintentando hasta que la cuenta vuelva a estar operativa.

## 13. Copias de seguridad

Con el bot y el trabajador detenidos, respalda de forma privada:

- `marketplace_bot.db`.
- `ArticulosGenerados.xlsx`.
- `imagenes_firupost`.
- `marketplace_custom_products`.
- `marketplace_accounts.json`.

Cada socio debe guardar su respaldo en un espacio privado. No debe entregar a
otros socios `marketplace_bot.db`, `.firupost_openai_key.bin`, tokens, logs ni
su carpeta `%LOCALAPPDATA%\MarketplaceBot_*`.

La clave de OpenAI guardada desde el panel queda protegida por Windows para el
usuario que la configuro. No copies ese archivo cifrado a otra computadora; cada
socio debe configurar su propia clave o usar la redaccion local.
