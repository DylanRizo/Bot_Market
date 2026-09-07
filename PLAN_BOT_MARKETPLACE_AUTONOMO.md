# Plan de desarrollo: bot autonomo de Facebook Marketplace

## Estado de implementacion

Las fases 0 a 8 fueron implementadas el 30 de agosto de 2026. El sistema queda
en modo `supervised`, con el trabajador instalado en Windows y el interruptor
`Bot activo` apagado. La activacion de publicaciones reales se realiza desde el
panel despues de revisar el calendario.

Extensiones posteriores implementadas:

- Historial de fotos separado por cuenta y bloqueo de contenido repetido.
- Seleccion de varias cuentas con rotacion o publicacion en todas.
- Productos manuales independientes del inventario principal.

## Objetivo final

Convertir el bot actual en un sistema que pueda preparar, calendarizar y publicar
productos durante toda la semana con una configuracion minima.

El usuario solo debera definir:

1. Cuenta de Marketplace.
2. Dias y horario permitidos.
3. Maximo de publicaciones por dia.
4. Separacion entre publicaciones.
5. Nivel de autonomia.
6. Productos que deben excluirse, si existen.

El sistema seleccionara productos, resolvera sus fotos, elegira el modo de
agrupacion, generara descripcion y etiquetas, evitara repeticiones, publicara en
el horario correcto y registrara el resultado.

## Principios del proyecto

- No guardar la contrasena de Facebook. Se reutiliza un perfil de Chrome con la
  sesion iniciada.
- Nunca publicar un producto sin fotos, precio, categoria y descripcion validos.
- Una falla no debe detener el calendario completo.
- No crear publicaciones duplicadas por reintentos o reinicios.
- Todas las acciones automaticas deben quedar en el historial.
- Captchas, verificaciones de identidad y bloqueos de Facebook requieren
  intervencion humana.
- La configuracion tecnica se mantiene oculta en una seccion avanzada.

## Fase 0: estabilizar la base actual

### Objetivo

Dejar el publicador actual como una pieza confiable y comprobable antes de
agregar ejecucion permanente.

### Trabajo

- Centralizar rutas, cuenta, perfil de Chrome y valores predeterminados.
- Crear una prueba de regresion para titulo, precio, categoria, condicion,
  descripcion, etiquetas, fotos y metodos de entrega.
- Clasificar los errores del publicador: sesion, formulario, datos, imagenes,
  Facebook y error interno.
- Guardar captura y diagnostico cuando falle un campo.
- Evitar que dos ejecuciones controlen la misma cuenta al mismo tiempo.
- Conservar los modos `plan`, `dry-run` y `publish`.

### Terminada cuando

- Un dry-run de un producto completa todos los campos.
- Un fallo informa la causa y el campo exactos.
- Una segunda ejecucion simultanea es rechazada de forma segura.

## Fase 1: almacenamiento persistente

### Objetivo

Reemplazar el estado temporal de los JSON por una base local que sobreviva a
reinicios y permita consultar el historial sin perder datos.

### Trabajo

- Crear `marketplace_bot.db` con SQLite.
- Guardar productos, variantes, cuentas, configuracion, calendario,
  publicaciones e intentos.
- Importar la configuracion y actividad de los JSON actuales.
- Mantener exportacion JSON para respaldo y diagnostico.
- Asignar un identificador unico a cada publicacion planeada.
- Guardar una huella del producto, fotos, precio y cuenta para impedir
  publicaciones duplicadas.

### Estados del calendario

- `planned`: creado por el planificador.
- `queued`: listo para su horario.
- `running`: el publicador lo esta procesando.
- `published`: confirmado como publicado.
- `retry`: pendiente de otro intento.
- `blocked`: necesita intervencion.
- `skipped`: omitido por una regla.
- `cancelled`: cancelado por el usuario.

### Terminada cuando

- Reiniciar el panel no elimina ni duplica trabajos.
- Se puede reconstruir que ocurrio con cualquier publicacion.

## Fase 2: calendario semanal persistente

### Objetivo

Generar automaticamente una semana de publicaciones y ejecutarla sin dejar un
proceso esperando durante horas.

### Trabajo

- Agregar reglas de dias, hora inicial, hora final, maximo diario e intervalo.
- Generar espacios disponibles para siete dias.
- Permitir una hora inicial inmediata o una fecha concreta.
- Crear una cola ordenada por fecha y prioridad.
- Recuperar trabajos que quedaron `running` despues de un reinicio.
- Permitir pausar todo el calendario, una cuenta o un producto.
- Recalcular los trabajos futuros sin alterar publicaciones ya completadas.

### Terminada cuando

- El calendario de una semana se genera con un boton.
- Apagar y encender la computadora conserva la proxima publicacion.
- El bot nunca publica fuera del horario configurado.

## Fase 3: seleccion y rotacion inteligente de productos

### Objetivo

Permitir que el usuario no tenga que escoger manualmente cada articulo.

### Reglas del selector

- Solo usar productos activos, con precio y fotos validas.
- Excluir productos sin existencia cuando el inventario la controle.
- Priorizar los que llevan mas tiempo sin publicarse.
- Aplicar un periodo minimo antes de repetir familia, variante o anuncio.
- Alternar familias de productos durante la semana.
- Respetar productos fijados, excluidos y prioridades manuales.
- Elegir agrupado, por color, por talla o individual segun las variantes.
- No mezclar productos con precios incompatibles en una publicacion agrupada.
- Limitar la cantidad de fotos al maximo admitido por el formulario.

### Resultado esperado

Cada espacio del calendario recibe automaticamente producto, formato, cuenta,
precio, fotos, descripcion y etiquetas.

### Terminada cuando

- Puede crear una semana completa sin seleccionar productos uno por uno.
- No repite un producto dentro del periodo configurado.
- Explica en el panel por que eligio u omitio cada producto.

## Fase 4: recuperacion automatica y vigilancia de sesion

### Objetivo

Evitar que un fallo aislado detenga el trabajo del resto de la semana.

### Trabajo

- Verificar sesion, Marketplace, conexion, imagenes y datos antes de publicar.
- Reintentar errores temporales con espera progresiva y limite configurable.
- Posponer un producto fallido y continuar con el siguiente espacio disponible.
- Detectar sesion cerrada, captcha, 2FA, revision o bloqueo de la cuenta.
- Marcar esos casos como `blocked` sin seguir intentando indefinidamente.
- Reiniciar el navegador controlado cuando el puerto deje de responder.
- Guardar capturas, URL, etapa y mensaje del ultimo error.
- Verificar el resultado final antes de marcar `published`.

### Terminada cuando

- Un fallo temporal se recupera sin intervencion.
- Un bloqueo de Facebook pausa solo la cuenta afectada.
- No se publica dos veces por un reintento.

## Fase 5: interfaz simplificada

### Objetivo

Ocultar la configuracion tecnica y convertir el panel en un centro de control
comprensible para el negocio.

### Pantalla principal

- Interruptor `Bot activo`.
- Estado de cada cuenta.
- Proxima publicacion y cuenta seleccionada.
- Publicaciones realizadas hoy y limite diario.
- Alertas que necesitan atencion.
- Botones `Pausar`, `Publicar ahora` y `Generar semana`.

### Calendario

- Vista semanal con producto, foto, hora, cuenta y estado.
- Arrastrar para cambiar una hora.
- Reemplazar, editar, omitir o bloquear un producto.
- Vista previa completa antes de la publicacion.

### Configuracion simple

- Dias activos.
- Rango de horas.
- Maximo diario.
- Intervalo.
- Cuenta.
- Nivel de autonomia.

Las rutas, puertos, archivos, perfil de Chrome y opciones internas quedaran en
`Configuracion avanzada`.

### Terminada cuando

- Una persona puede activar una semana sin conocer rutas ni archivos JSON.
- Fotos y precios se modifican desde la vista previa.

## Fase 6: trabajador permanente en Windows

### Objetivo

Ejecutar el calendario aunque el panel web no permanezca abierto.

### Trabajo

- Crear un proceso `marketplace_scheduler_worker.py`.
- Consultar periodicamente la cola persistente.
- Ejecutar un solo trabajo por cuenta a la vez.
- Registrar una senal de salud del trabajador.
- Iniciarlo automaticamente con el Programador de tareas de Windows.
- Reiniciarlo si se cierra inesperadamente.
- Mantener el panel como interfaz, no como responsable de la ejecucion.

### Terminada cuando

- Cerrar el navegador del panel no detiene el calendario.
- Reiniciar Windows recupera automaticamente la programacion pendiente.

## Fase 7: alertas y resumen del negocio

### Objetivo

Pedir atencion solamente cuando sea necesaria.

### Trabajo

- Alertas locales por sesion vencida, captcha, producto invalido o cuenta
  bloqueada.
- Resumen diario con publicadas, omitidas, fallidas y pendientes.
- Resumen semanal por producto y cuenta.
- Boton para reintentar una publicacion bloqueada despues de resolver Facebook.
- Preparar conectores opcionales para correo o mensajeria.

### Terminada cuando

- El usuario puede saber en menos de un minuto si la semana marcha bien.
- Los errores importantes generan una alerta visible y accionable.

## Fase 8: despliegue progresivo

La autonomia se habilitara por niveles para reducir errores y publicaciones no
deseadas.

### Nivel 1: simulacion

- Generar siete dias de calendario.
- Ejecutar validaciones sin abrir Facebook.
- Revisar rotacion, precios, fotos y agrupaciones.

### Nivel 2: dry-run

- Completar formularios reales sin presionar `Next` o `Publish`.
- Probar distintos productos, categorias y cuentas.

### Nivel 3: supervisado

- Publicar una cantidad diaria pequena.
- Revisar cada resultado y ajustar reglas.

### Nivel 4: semiautomatico

- El usuario aprueba el calendario semanal una sola vez.
- El trabajador publica cada elemento aprobado.

### Nivel 5: autonomo

- El bot genera y ejecuta el calendario.
- Solo se detiene ante limites, datos incompletos o bloqueos de Facebook.

### Terminada cuando

- Completa una semana supervisada sin duplicados ni publicaciones incorrectas.
- Completa una semana semiautomatica con recuperacion ante fallos temporales.
- El usuario autoriza expresamente activar el nivel autonomo.

## Orden de implementacion

1. Fase 0: estabilizacion.
2. Fase 1: SQLite e historial.
3. Fase 2: calendario persistente.
4. Fase 3: rotacion de productos.
5. Fase 4: recuperacion y vigilancia.
6. Fase 5: interfaz simplificada.
7. Fase 6: trabajador de Windows.
8. Fase 7: alertas y reportes.
9. Fase 8: despliegue progresivo.

## Definicion de terminado del proyecto

El proyecto estara listo cuando el usuario pueda:

1. Iniciar sesion una vez en cada cuenta.
2. Elegir dias, horario, limite e intervalo.
3. Pulsar `Generar semana`.
4. Revisar o aprobar el calendario, segun el nivel de autonomia.
5. Activar el bot y cerrar el panel.
6. Consultar despues el historial y las alertas.

El bot debera continuar tras reinicios, no duplicar anuncios, respetar limites y
detener una cuenta cuando Facebook solicite una accion humana.
