# Modos de publicacion Marketplace

En cada job de `marketplace_campaign_example.json` puedes elegir como publicar los productos con `listing_mode`.

## 1. Individual

Publica cada talla/color como un anuncio separado.

```json
{
  "name": "Camisas sin mangas individuales",
  "listing_mode": "individual",
  "source_excel": "ArticulosGenerados.xlsx",
  "images_root": "imagenes_firupost",
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
  "images_root": "imagenes_firupost",
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
  "images_root": "imagenes_firupost",
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
  "images_root": "imagenes_firupost",
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

Antes de publicar, revisa el plan:

```powershell
python "C:\ruta\al\bot\marketplace_campaign_runner.py" --plan-only
```

Para publicar de verdad respetando los intervalos:

```powershell
python "C:\ruta\al\bot\marketplace_campaign_runner.py" --confirm-publish
```
