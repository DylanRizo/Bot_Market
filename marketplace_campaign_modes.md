# Modos de publicacion Marketplace

> Esto describe las **campanas JSON manuales** que ejecuta
> `marketplace_campaign_runner.py`. El uso normal es el panel y el calendario
> automatico (ver [`MANUAL_OPERACION.md`](MANUAL_OPERACION.md)), que eligen el
> modo por familia de producto. El archivo de campana no viene en el repositorio:
> creas el tuyo (`marketplace_campaign_example.json` es el nombre por defecto y
> esta ignorado por git) o lo indicas con `--campaign`.

Una campana es un objeto con `default_account` (clave de `marketplace_accounts.json`)
y una lista `jobs`. En cada job eliges como publicar los productos con `listing_mode`:

```json
{
  "default_account": "cuenta1",
  "jobs": [ { "name": "...", "listing_mode": "individual", "source_excel": "ArticulosGenerados.xlsx", "images_root": "imagenes_productos" } ]
}
```

## 1. Individual

Publica cada talla/color como un anuncio separado.

```json
{
  "name": "Camisas sin mangas individuales",
  "listing_mode": "individual",
  "source_excel": "ArticulosGenerados.xlsx",
  "images_root": "imagenes_productos",
  "title_contains": ["sin mangas"],
  "item_interval_minutes": 60
}
```

## 2. Agrupado

Publica todos los productos seleccionados en un solo anuncio, con varias fotos y una descripcion que lista colores/tallas.

```json
{
  "name": "Camisas manga corta agrupadas",
  "listing_mode": "grouped",
  "source_excel": "ArticulosGenerados.xlsx",
  "images_root": "imagenes_productos",
  "sku_prefixes": ["TS"]
}
```

## 3. Agrupado Por Color

Hace un anuncio por cada color. Ejemplo: una publicacion para negro con tallas S/M/L, otra para blanco con tallas S/M/L.

```json
{
  "name": "Camisas sin mangas agrupadas por color",
  "listing_mode": "grouped",
  "group_by": "color",
  "source_excel": "ArticulosGenerados.xlsx",
  "images_root": "imagenes_productos",
  "title_contains": ["sin mangas"],
  "item_interval_minutes": 60
}
```

## 4. Agrupado Por Talla

Hace un anuncio por cada talla. Ejemplo: una publicacion talla M con colores negro/blanco/gris.

```json
{
  "name": "Camisas por talla",
  "listing_mode": "grouped",
  "group_by": "size",
  "source_excel": "ArticulosGenerados.xlsx",
  "images_root": "imagenes_productos",
  "sku_prefixes": ["TS"],
  "item_interval_minutes": 60
}
```

## Seleccion De Productos

Puedes seleccionar productos con cualquiera de estas opciones:

- `row_indices`: indices exactos del Excel, por ejemplo `[1, 2, 3]`.
- `skus`: SKUs exactos, por ejemplo `["TSBK-M", "TSBK-L"]`.
- `sku_prefixes`: prefijos de SKU, por ejemplo `["TS"]`.
- `title_contains`: texto dentro del titulo, por ejemplo `["sin mangas"]`.
- `exclude_title_contains`: texto a excluir, por ejemplo `["sin mangas"]`.

Antes de publicar, revisa el plan (no abre Facebook):

```powershell
python marketplace_campaign_runner.py --plan-only
```

Para llenar el formulario y avanzar con `Next` sin publicar:

```powershell
python marketplace_campaign_runner.py --advance-only
```

Para publicar de verdad respetando los intervalos:

```powershell
python marketplace_campaign_runner.py --confirm-publish
```

Otras opciones: `--campaign RUTA`, `--accounts RUTA`, `--from-job N`,
`--max-jobs N` y `--fast` (ignora las esperas; solo para pruebas). El runner
tambien respeta la proteccion de cuenta y el candado de cada cuenta.
