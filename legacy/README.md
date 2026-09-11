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
