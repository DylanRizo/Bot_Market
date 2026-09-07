from __future__ import annotations

import argparse
import html
import json
import logging
import os
import re
import shlex
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

import pandas as pd
import requests
from PIL import Image

# pyrefly: ignore [missing-import]
import gspread
# pyrefly: ignore [missing-import]
from google.auth.transport.requests import Request
# pyrefly: ignore [missing-import]
from google.oauth2.credentials import Credentials
# pyrefly: ignore [missing-import]
from google_auth_oauthlib.flow import InstalledAppFlow


logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")


SCRATCH_DIR = Path(__file__).resolve().parent
DEFAULT_IMAGES_DIR = SCRATCH_DIR / "imagenes_firupost"
DEFAULT_EXISTING_EXCEL = SCRATCH_DIR / "Articulos_FiruPost.xlsx"
DEFAULT_OUTPUT_EXCEL = SCRATCH_DIR / "ArticulosGenerados.xlsx"
DEFAULT_REPORT_EXCEL = SCRATCH_DIR / "firupost_validation_report.xlsx"
DEFAULT_PROMPT_FILE = SCRATCH_DIR / "prompt_firupost_ia.txt"

FIRUPOST_COLUMNS = [
    "Titulo",
    "Precio",
    "Categoria",
    "Condicion",
    "Descripcion",
    "Etiqueta",
    "Sku",
    "Ubicacion",
    "CarpetaImg",
    "NombreImg",
]

IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp"}

DEFAULT_WP_URL = "https://lacomarcanic.com/"
DEFAULT_WP_CONSUMER_KEY = os.getenv("WP_CONSUMER_KEY", "ck_8be7948184af5e0568d1333703aa14f44ac8ae3f")
DEFAULT_WP_CONSUMER_SECRET = os.getenv("WP_CONSUMER_SECRET", "cs_918cdb15ef324f202e5cae41ab7211c173864c57")
DEFAULT_GOOGLE_CREDENTIALS = SCRATCH_DIR / "credenciales_google.json"
DEFAULT_SHEET_ID = os.getenv("GOOGLE_SHEET_ID", "1_WdxwD77PPS_gaQgzhYsyfURwVTn_4LIKghBVrdDiw0")


@dataclass
class ImageChoice:
    folder_name: str
    image_stem: str
    image_path: Path | None
    valid: bool
    reason: str


def text_value(value: Any) -> str:
    if value is None:
        return ""
    try:
        if pd.isna(value):
            return ""
    except TypeError:
        pass
    return str(value).strip()


def number_value(value: Any, default: float = 0.0) -> float:
    raw = text_value(value)
    if not raw:
        return default
    raw = raw.replace("C$", "").replace("$", "").replace(",", "").strip()
    try:
        return float(raw)
    except ValueError:
        return default


def first_value(row: pd.Series | dict[str, Any], names: Iterable[str]) -> Any:
    for name in names:
        if name in row:
            value = row[name]
            if text_value(value):
                return value
    return ""


def strip_html(value: str) -> str:
    cleaned = re.sub(r"<br\s*/?>", "\n", value or "", flags=re.IGNORECASE)
    cleaned = re.sub(r"<[^>]+>", " ", cleaned)
    cleaned = html.unescape(cleaned)
    cleaned = re.sub(r"\s+", " ", cleaned)
    return cleaned.strip()


def clean_title(value: str) -> str:
    value = text_value(value)
    value = value.replace("´", "'").replace("¨", '"')
    value = re.sub(r"\s+", " ", value)
    return value.strip(" -")


def derive_sku(row: pd.Series | dict[str, Any]) -> str:
    sku = text_value(first_value(row, ["Sku", "SKU", "sku", "Codigo", "Codigo unico del producto", "codigo unico del producto"]))
    if sku:
        return sku

    folder = text_value(first_value(row, ["CarpetaImg", "carpeta_imagenes", "Carpeta Imagenes", "carpeta"]))
    if folder:
        return Path(folder).name

    title = clean_title(text_value(first_value(row, ["Titulo", "titulo", "Producto", "producto"])))
    return re.sub(r"[^A-Za-z0-9_-]+", "-", title).strip("-")[:40] or "SIN-SKU"


def infer_category(title: str, fallback: str = "Ropa y accesorios") -> str:
    normalized = title.lower()
    if any(word in normalized for word in ["mochila", "bolso", "cartera"]):
        return "Bolsos y equipaje"
    if any(word in normalized for word in ["zapato", "tenis", "bota", "calzado"]):
        return "Ropa y accesorios"
    return fallback


def infer_tag(title: str, sku: str, fallback: str = "") -> str:
    title_low = title.lower()
    if "camisa" in title_low:
        return "camisa"
    if "chaleco" in title_low:
        return "chaleco"
    if "durag" in title_low:
        return "durag"
    if "short" in title_low:
        return "short"
    if "mochila" in title_low:
        return "mochila"
    if fallback:
        return fallback
    return sku.split("-")[0].lower() if sku else "producto"


def infer_variant_notes(title: str) -> str:
    notes: list[str] = []
    if "," in title:
        after_comma = title.rsplit(",", 1)[-1].strip()
        if after_comma:
            notes.append(f"Talla: {after_comma}.")
    if " - " in title:
        variant = title.rsplit(" - ", 1)[-1].strip()
        if variant and "," not in variant:
            notes.append(f"Color/variante: {variant}.")
    return " ".join(notes)


def sanitize_public_description(description: str) -> str:
    """Remove internal inventory details that should not be visible to buyers."""
    cleaned_lines: list[str] = []
    for raw_line in strip_html(description).splitlines():
        line = raw_line.strip()
        if not line:
            cleaned_lines.append("")
            continue
        lowered = line.lower()
        if lowered.startswith("sku:") or lowered.startswith("stock disponible:"):
            continue
        line = re.sub(r"\s*\(sku\s+[^)]+\)", "", line, flags=re.IGNORECASE)
        line = re.sub(r":\s*stock\s+\d+\b", "", line, flags=re.IGNORECASE)
        line = re.sub(r"\bstock\s+disponible\s*:\s*\d+\.?", "", line, flags=re.IGNORECASE)
        line = re.sub(r"\bsku\s*:\s*[\w-]+\.?", "", line, flags=re.IGNORECASE)
        line = line.strip()
        if line:
            cleaned_lines.append(line)
    return "\n".join(cleaned_lines).strip()


def sales_intro_for_title(title: str, condition: str) -> str:
    lower_title = title.lower()
    if "leggin" in lower_title:
        return (
            f"{title}. Producto nuevo con estilo deportivo y corte favorecedor, "
            "ideal para entrenar, caminar o combinar en un look casual."
        )
    if "sin mangas" in lower_title:
        return (
            f"{title}. Camisa de compresion nueva sin mangas, comoda para entrenar "
            "con libertad de movimiento y un look limpio."
        )
    if "camisa de compresion" in lower_title or "compresión" in lower_title:
        return (
            f"{title}. Camisa de compresion nueva, perfecta para entrenar, usar bajo otra prenda "
            "o armar un outfit deportivo."
        )
    condition_text = "nuevo" if condition.lower() in {"new", "nuevo"} else condition.lower()
    return (
        f"{title}. Producto {condition_text} disponible para entrega, ideal para uso diario "
        "o para completar tu estilo."
    )


def generate_marketplace_description(
    title: str,
    price: float,
    condition: str,
    sku: str,
    location: str,
    whatsapp: str,
    stock: str = "",
    existing_description: str = "",
    currency: str = "C$",
) -> str:
    existing_description = sanitize_public_description(existing_description)
    condition = condition or "New"
    variant_notes = infer_variant_notes(title)
    price_text = f"{currency}{int(price) if float(price).is_integer() else price:g}" if price else "consultar"

    if existing_description and len(existing_description) > 25:
        base = existing_description
    else:
        base = sales_intro_for_title(title, condition)

    lines = [
        base,
        variant_notes,
        f"Precio: {price_text}.",
        f"Ubicacion: {location}.",
        "Escribeme para confirmar talla/color y coordinar la entrega.",
    ]
    if whatsapp:
        lines.append(f"Tambien puedes escribir por WhatsApp: {whatsapp}.")

    return "\n".join(line for line in lines if line).strip()


def generate_marketplace_description_with_ai(
    ai_command: str,
    title: str,
    price: float,
    condition: str,
    sku: str,
    location: str,
    whatsapp: str,
    stock: str = "",
    existing_description: str = "",
    currency: str = "C$",
    timeout_seconds: int = 45,
) -> str:
    """Calls an optional external AI command.

    The command receives JSON on stdin and should print the final marketplace
    description on stdout. This keeps the bot provider-neutral: the command can
    be a Gemini/OpenAI/Claude wrapper, a local model, or any internal script.
    """
    if not ai_command:
        return ""

    payload = {
        "instruction": (
            "Escribe una descripcion vendedora para Facebook Marketplace en espanol. "
            "Debe ser clara, breve, honesta, sin inventar datos y maximo 7 lineas. "
            "Incluye precio, ubicacion y llamado a escribir para confirmar talla/color. "
            "No incluyas SKU, codigos internos ni cantidades exactas de stock."
        ),
        "product": {
            "titulo": title,
            "precio": price,
            "moneda": currency,
            "condicion": condition,
            "sku": sku,
            "ubicacion": location,
            "whatsapp": whatsapp,
            "stock": stock,
            "descripcion_existente": sanitize_public_description(existing_description),
        },
    }
    try:
        command_args = shlex.split(ai_command)
        completed = subprocess.run(
            command_args,
            input=json.dumps(payload, ensure_ascii=False),
            text=True,
            capture_output=True,
            shell=False,
            timeout=timeout_seconds,
            cwd=str(SCRATCH_DIR),
        )
        if completed.returncode != 0:
            logging.warning("AI command fallo para SKU %s: %s", sku, completed.stderr.strip())
            return ""
        description = completed.stdout.strip()
        if len(description) < 20:
            logging.warning("AI command devolvio descripcion muy corta para SKU %s", sku)
            return ""
        return description
    except Exception as exc:  # noqa: BLE001 - external AI is optional.
        logging.warning("No se pudo generar descripcion con IA para SKU %s: %s", sku, exc)
        return ""


def is_valid_image(path: Path) -> tuple[bool, str]:
    if not path.exists():
        return False, "no existe"
    if path.stat().st_size < 1024:
        return False, f"archivo demasiado pequeno ({path.stat().st_size} bytes)"
    try:
        with Image.open(path) as img:
            img.verify()
        return True, "ok"
    except Exception as exc:  # noqa: BLE001 - report validation details.
        return False, str(exc)


def resolve_folder(images_root: Path, folder_value: str, sku: str) -> tuple[str, Path]:
    folder_value = text_value(folder_value)
    if folder_value:
        folder_path = Path(folder_value)
        if folder_path.is_absolute():
            return folder_path.name, folder_path
        return folder_path.name, images_root / folder_path
    return sku, images_root / sku


def choose_image(images_root: Path, folder_value: str, image_value: str, sku: str) -> ImageChoice:
    folder_name, folder_path = resolve_folder(images_root, folder_value, sku)
    image_stem = Path(text_value(image_value)).stem

    if not folder_path.exists():
        return ImageChoice(folder_name, image_stem, None, False, f"carpeta no existe: {folder_path}")

    candidates: list[Path] = []
    if image_stem:
        for ext in IMAGE_EXTENSIONS:
            candidate = folder_path / f"{image_stem}{ext}"
            if candidate.exists():
                candidates.append(candidate)

    if not candidates:
        candidates = sorted(
            path for path in folder_path.iterdir() if path.is_file() and path.suffix.lower() in IMAGE_EXTENSIONS
        )

    if not candidates:
        return ImageChoice(folder_name, image_stem, None, False, f"sin imagenes en {folder_path}")

    first_failure = ""
    for candidate in candidates:
        valid, reason = is_valid_image(candidate)
        if valid:
            return ImageChoice(folder_name, candidate.stem, candidate, True, "ok")
        if not first_failure:
            first_failure = f"{candidate.name}: {reason}"

    return ImageChoice(folder_name, candidates[0].stem, candidates[0], False, first_failure)


def build_firupost_row(
    row: pd.Series,
    images_root: Path,
    default_location: str,
    default_category: str,
    whatsapp: str,
    currency: str,
    ai_description_command: str = "",
) -> tuple[dict[str, Any], dict[str, Any]]:
    title = clean_title(text_value(first_value(row, ["Titulo", "titulo", "Nombre", "name", "Producto", "producto"])))
    sku = derive_sku(row)
    price = number_value(first_value(row, ["Precio", "precio", "price"]), 0.0)
    condition = text_value(first_value(row, ["Condicion", "estado", "condition"])) or "New"
    category = text_value(first_value(row, ["Categoria", "categoria", "category"])) or infer_category(title, default_category)
    tag = text_value(first_value(row, ["Etiqueta", "etiquetas", "tag"])) or infer_tag(title, sku)
    location = text_value(first_value(row, ["Ubicacion", "ubicacion", "location"]))
    if not location or location == "0":
        location = default_location

    stock = text_value(first_value(row, ["Stock", "stock", "cantidad de entrada del producto", "cantidad"]))
    existing_description = text_value(first_value(row, ["Descripcion", "descripcion", "description", "short_description"]))
    description = generate_marketplace_description_with_ai(
        ai_command=ai_description_command,
        title=title,
        price=price,
        condition=condition,
        sku=sku,
        location=location,
        whatsapp=whatsapp,
        stock=stock,
        existing_description=existing_description,
        currency=currency,
    )
    if not description:
        description = generate_marketplace_description(
            title=title,
            price=price,
            condition=condition,
            sku=sku,
            location=location,
            whatsapp=whatsapp,
            stock=stock,
            existing_description=existing_description,
            currency=currency,
        )

    image = choose_image(
        images_root=images_root,
        folder_value=text_value(first_value(row, ["CarpetaImg", "carpeta_imagenes", "carpeta"])),
        image_value=text_value(first_value(row, ["NombreImg", "nombre_img", "imagen"])),
        sku=sku,
    )
    sync_error = text_value(first_value(row, ["SyncError", "sync_error"]))

    firupost_row = {
        "Titulo": title,
        "Precio": price,
        "Categoria": category,
        "Condicion": condition,
        "Descripcion": description,
        "Etiqueta": tag,
        "Sku": sku,
        "Ubicacion": location,
        "CarpetaImg": image.folder_name,
        "NombreImg": image.image_stem,
    }

    report_row = {
        **firupost_row,
        "Publicable": image.valid and bool(title) and price > 0 and not sync_error,
        "ImagenValida": image.valid,
        "ImagenPath": str(image.image_path) if image.image_path else "",
        "Validacion": sync_error or image.reason,
    }
    return firupost_row, report_row


def write_ai_prompt(path: Path, location: str, whatsapp: str) -> None:
    whatsapp_line = f"WhatsApp del negocio: {whatsapp}." if whatsapp else "Si el cliente quiere cerrar compra, pide su numero de WhatsApp."
    prompt = f"""Eres un vendedor amable y directo de Facebook Marketplace para La Comarca.

Datos del producto:
- Titulo: {{Titulo}}
- Precio: {{Precio}}
- Condicion: {{Condicion}}
- Descripcion: {{Descripcion}}
- Stock: {{Stock}}
- Ubicacion: {{Ubicacion}}
- SKU: {{Sku}}

Reglas:
- Responde siempre en espanol claro, natural y breve.
- Maximo 4 lineas por respuesta.
- Confirma disponibilidad antes de prometer entrega.
- No inventes tallas, colores, descuentos, garantia ni envio si no aparecen en los datos.
- Si preguntan por precio, responde con {{Precio}} y ofrece coordinar entrega en {location}.
- {whatsapp_line}
- Si el articulo no aparece en el Excel, responde: "Por ahora no tengo ese producto disponible, pero puedes revisar mi perfil o escribirme para ver opciones similares."
"""
    path.write_text(prompt, encoding="utf-8")
    logging.info("Prompt de IA escrito en: %s", path)


def convert_existing_excel(
    input_excel: Path,
    images_root: Path,
    output_excel: Path,
    report_excel: Path,
    default_location: str,
    default_category: str,
    whatsapp: str,
    currency: str,
    include_invalid: bool,
    allow_duplicates: bool,
    ai_description_command: str = "",
) -> tuple[pd.DataFrame, pd.DataFrame]:
    if not input_excel.exists():
        raise FileNotFoundError(f"No existe el Excel de entrada: {input_excel}")
    df = pd.read_excel(input_excel)
    if df.empty:
        raise ValueError(f"El Excel de entrada esta vacio: {input_excel}")

    firupost_rows: list[dict[str, Any]] = []
    report_rows: list[dict[str, Any]] = []
    seen_skus: set[str] = set()
    for _, row in df.iterrows():
        firupost_row, report_row = build_firupost_row(
            row=row,
            images_root=images_root,
            default_location=default_location,
            default_category=default_category,
            whatsapp=whatsapp,
            currency=currency,
            ai_description_command=ai_description_command,
        )

        sku_key = text_value(firupost_row.get("Sku")).upper()
        if sku_key and not allow_duplicates:
            if sku_key in seen_skus:
                report_row["Publicable"] = False
                report_row["Validacion"] = "SKU duplicado; omitido para evitar publicaciones repetidas"
            else:
                seen_skus.add(sku_key)

        report_rows.append(report_row)
        if include_invalid or report_row["Publicable"]:
            firupost_rows.append(firupost_row)

    firupost_df = pd.DataFrame(firupost_rows, columns=FIRUPOST_COLUMNS)
    report_df = pd.DataFrame(report_rows)

    firupost_df.to_excel(output_excel, index=False)
    report_df.to_excel(report_excel, index=False)

    logging.info("Excel FiruPost escrito en: %s", output_excel)
    logging.info("Reporte de validacion escrito en: %s", report_excel)
    logging.info("Productos publicables: %s/%s", len(firupost_df), len(report_df))
    return firupost_df, report_df


class FiruPostAutomator:
    def __init__(
        self,
        wp_url: str,
        wp_consumer_key: str,
        wp_consumer_secret: str,
        google_credentials_file: Path,
        sheet_id: str,
        images_dir: Path = DEFAULT_IMAGES_DIR,
    ):
        self.wp_url = wp_url.rstrip("/")
        self.wp_consumer_key = wp_consumer_key
        self.wp_consumer_secret = wp_consumer_secret
        self.google_credentials_file = google_credentials_file
        self.sheet_id = sheet_id
        self.images_dir = images_dir
        self.images_dir.mkdir(parents=True, exist_ok=True)
        self.gc = self._connect_google_sheets()

    def _connect_google_sheets(self):
        try:
            scopes = ["https://www.googleapis.com/auth/spreadsheets.readonly"]
            creds = None
            token_path = SCRATCH_DIR / "token_google.json"
            if token_path.exists():
                try:
                    creds = Credentials.from_authorized_user_file(str(token_path), scopes)
                except Exception as token_err:  # noqa: BLE001
                    logging.warning("No se pudo cargar token_google.json: %s", token_err)

            if not creds or not creds.valid:
                if creds and creds.expired and creds.refresh_token:
                    try:
                        creds.refresh(Request())
                    except Exception as refresh_err:  # noqa: BLE001
                        logging.warning("No se pudo refrescar el token: %s", refresh_err)
                        creds = None

                if not creds:
                    flow = InstalledAppFlow.from_client_secrets_file(str(self.google_credentials_file), scopes=scopes)
                    creds = flow.run_local_server(port=0)

                token_path.write_text(creds.to_json(), encoding="utf-8")
                logging.info("Credenciales de Google guardadas en %s", token_path)

            client = gspread.authorize(creds)
            logging.info("Conexion exitosa a Google Sheets.")
            return client
        except Exception as exc:  # noqa: BLE001
            logging.error("Error conectando a Google Sheets: %s", exc)
            return None

    def fetch_inventory_from_sheets(self, worksheet_name: str = "Inventario") -> pd.DataFrame:
        logging.info("Obteniendo inventario desde Google Sheets...")
        if not self.gc:
            logging.error("No hay conexion activa con Google Sheets.")
            return pd.DataFrame()
        try:
            sheet = self.gc.open_by_key(self.sheet_id).worksheet(worksheet_name)
            data = sheet.get_all_records()
            return pd.DataFrame(data)
        except Exception as exc:  # noqa: BLE001
            logging.error("Error leyendo Google Sheets: %s", exc)
            return pd.DataFrame()

    def fetch_product_details_wp(self, sku: str) -> dict[str, Any] | None:
        logging.info("Consultando WooCommerce para SKU: %s", sku)
        try:
            response = requests.get(
                f"{self.wp_url}/wp-json/wc/v3/products",
                params={
                    "sku": sku,
                    "consumer_key": self.wp_consumer_key,
                    "consumer_secret": self.wp_consumer_secret,
                },
                timeout=30,
            )
            response.raise_for_status()
            products = response.json()
            return products[0] if products else None
        except Exception as exc:  # noqa: BLE001
            logging.error("Error consultando WooCommerce para SKU %s: %s", sku, exc)
            return None

    def download_images(self, product_id: str, image_urls: list[str], retries: int = 2) -> list[Path]:
        product_folder = self.images_dir / str(product_id)
        product_folder.mkdir(parents=True, exist_ok=True)
        local_paths: list[Path] = []

        headers = {"User-Agent": "Mozilla/5.0 FiruPostInventoryBot/1.0"}
        for index, url in enumerate(image_urls):
            file_path = product_folder / f"foto_{index + 1}.jpg"
            valid, _ = is_valid_image(file_path)
            if valid:
                local_paths.append(file_path)
                continue

            for attempt in range(retries + 1):
                try:
                    response = requests.get(url, headers=headers, timeout=40)
                    response.raise_for_status()
                    content_type = response.headers.get("content-type", "").lower()
                    if "image" not in content_type and not url.lower().split("?")[0].endswith(tuple(IMAGE_EXTENSIONS)):
                        raise ValueError(f"respuesta no es imagen: {content_type}")
                    file_path.write_bytes(response.content)
                    valid, reason = is_valid_image(file_path)
                    if not valid:
                        raise ValueError(reason)
                    local_paths.append(file_path)
                    logging.info("Imagen descargada: %s", file_path)
                    break
                except Exception as exc:  # noqa: BLE001
                    if attempt >= retries:
                        logging.error("No se pudo descargar imagen %s: %s", url, exc)
                    else:
                        time.sleep(1 + attempt)
        return local_paths

    def generate_from_inventory(
        self,
        inventory_df: pd.DataFrame,
        output_excel: Path,
        report_excel: Path,
        default_location: str,
        default_category: str,
        whatsapp: str,
        currency: str,
        include_invalid: bool,
        allow_duplicates: bool,
        ai_description_command: str,
    ) -> tuple[pd.DataFrame, pd.DataFrame]:
        if inventory_df is None or inventory_df.empty:
            raise ValueError("El inventario esta vacio.")

        grouped_inventory: dict[str, dict[str, Any]] = {}
        for _, row in inventory_df.iterrows():
            sku = derive_sku(row)
            if not sku or sku == "SIN-SKU":
                continue

            stock = number_value(first_value(row, ["cantidad de entrada del producto", "Stock", "stock"]), 0)
            if stock <= 0:
                continue

            if sku not in grouped_inventory:
                grouped_inventory[sku] = {"row": row, "stock": 0}
            grouped_inventory[sku]["stock"] += stock

        raw_rows: list[dict[str, Any]] = []
        for sku, grouped in grouped_inventory.items():
            row = grouped["row"]
            stock = grouped["stock"]
            price = number_value(first_value(row, ["precio", "Precio"]), 0)
            wp_data = self.fetch_product_details_wp(sku)
            if not wp_data:
                raw_rows.append(
                    {
                        "Titulo": first_value(row, ["titulo", "Titulo", "nombre", "Nombre"]) or sku,
                        "Precio": price,
                        "Categoria": default_category,
                        "Condicion": "New",
                        "Descripcion": "",
                        "Etiqueta": infer_tag(str(sku), str(sku)),
                        "Sku": sku,
                        "Ubicacion": default_location,
                        "CarpetaImg": sku,
                        "NombreImg": "",
                        "Stock": int(stock),
                        "WhatsApp": whatsapp,
                        "SyncError": "SKU no encontrado en WooCommerce",
                    }
                )
                continue

            images = [img.get("src", "") for img in wp_data.get("images", []) if isinstance(img, dict) and img.get("src")]
            self.download_images(sku, images)

            categories = wp_data.get("categories") or []
            category_name = ""
            if categories and isinstance(categories[0], dict):
                category_name = text_value(categories[0].get("name"))

            raw_rows.append(
                {
                    "Titulo": wp_data.get("name") or first_value(row, ["titulo", "Titulo"]) or sku,
                    "Precio": price,
                    "Categoria": category_name or default_category,
                    "Condicion": "New",
                    "Descripcion": strip_html(wp_data.get("short_description") or wp_data.get("description") or ""),
                    "Etiqueta": category_name,
                    "Sku": sku,
                    "Ubicacion": default_location,
                    "CarpetaImg": sku,
                    "NombreImg": "",
                    "Stock": int(stock),
                    "WhatsApp": whatsapp,
                }
            )

        tmp_df = pd.DataFrame(raw_rows)
        tmp_excel = SCRATCH_DIR / "_firupost_raw_sync.xlsx"
        tmp_df.to_excel(tmp_excel, index=False)
        return convert_existing_excel(
            input_excel=tmp_excel,
            images_root=self.images_dir,
            output_excel=output_excel,
            report_excel=report_excel,
            default_location=default_location,
            default_category=default_category,
            whatsapp=whatsapp,
            currency=currency,
            include_invalid=include_invalid,
            allow_duplicates=allow_duplicates,
            ai_description_command=ai_description_command,
        )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Prepara Excel, imagenes y prompt para FiruPost.")
    parser.add_argument("--source", choices=["existing", "sync"], default="existing")
    parser.add_argument("--input-excel", type=Path, default=DEFAULT_EXISTING_EXCEL)
    parser.add_argument("--images-root", type=Path, default=DEFAULT_IMAGES_DIR)
    parser.add_argument("--output-excel", type=Path, default=DEFAULT_OUTPUT_EXCEL)
    parser.add_argument("--report-excel", type=Path, default=DEFAULT_REPORT_EXCEL)
    parser.add_argument("--prompt-file", type=Path, default=DEFAULT_PROMPT_FILE)
    parser.add_argument("--location", default=os.getenv("FIRUPOST_LOCATION", "Managua, Nicaragua"))
    parser.add_argument("--category", default=os.getenv("FIRUPOST_CATEGORY", "Men's clothing & shoes"))
    parser.add_argument("--currency", default=os.getenv("FIRUPOST_CURRENCY", "C$"))
    parser.add_argument("--whatsapp", default=os.getenv("FIRUPOST_WHATSAPP", ""))
    parser.add_argument("--include-invalid", action="store_true", help="Incluye productos con imagen invalida en el Excel final.")
    parser.add_argument("--allow-duplicates", action="store_true", help="Permite publicar filas repetidas con el mismo SKU.")
    parser.add_argument(
        "--ai-description-command",
        default=os.getenv("FIRUPOST_AI_DESCRIPTION_COMMAND", ""),
        help="Comando externo opcional: recibe JSON por stdin y devuelve descripcion IA por stdout.",
    )
    parser.add_argument("--worksheet", default="Inventario")
    parser.add_argument("--wp-url", default=os.getenv("WP_URL", DEFAULT_WP_URL))
    parser.add_argument("--wp-consumer-key", default=DEFAULT_WP_CONSUMER_KEY)
    parser.add_argument("--wp-consumer-secret", default=DEFAULT_WP_CONSUMER_SECRET)
    parser.add_argument("--google-credentials", type=Path, default=DEFAULT_GOOGLE_CREDENTIALS)
    parser.add_argument("--sheet-id", default=DEFAULT_SHEET_ID)
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    args.output_excel.parent.mkdir(parents=True, exist_ok=True)
    args.report_excel.parent.mkdir(parents=True, exist_ok=True)
    args.prompt_file.parent.mkdir(parents=True, exist_ok=True)

    if args.source == "existing":
        firupost_df, report_df = convert_existing_excel(
            input_excel=args.input_excel,
            images_root=args.images_root,
            output_excel=args.output_excel,
            report_excel=args.report_excel,
            default_location=args.location,
            default_category=args.category,
            whatsapp=args.whatsapp,
            currency=args.currency,
            include_invalid=args.include_invalid,
            allow_duplicates=args.allow_duplicates,
            ai_description_command=args.ai_description_command,
        )
    else:
        automator = FiruPostAutomator(
            wp_url=args.wp_url,
            wp_consumer_key=args.wp_consumer_key,
            wp_consumer_secret=args.wp_consumer_secret,
            google_credentials_file=args.google_credentials,
            sheet_id=args.sheet_id,
            images_dir=args.images_root,
        )
        inventory_df = automator.fetch_inventory_from_sheets(args.worksheet)
        firupost_df, report_df = automator.generate_from_inventory(
            inventory_df=inventory_df,
            output_excel=args.output_excel,
            report_excel=args.report_excel,
            default_location=args.location,
            default_category=args.category,
            whatsapp=args.whatsapp,
            currency=args.currency,
            include_invalid=args.include_invalid,
            allow_duplicates=args.allow_duplicates,
            ai_description_command=args.ai_description_command,
        )

    write_ai_prompt(args.prompt_file, args.location, args.whatsapp)

    invalid = report_df[~report_df["Publicable"]]
    print()
    print(f"Excel listo: {args.output_excel}")
    print(f"Reporte: {args.report_excel}")
    print(f"Prompt IA: {args.prompt_file}")
    print(f"Productos en Excel final: {len(firupost_df)}")
    print(f"Productos con problemas: {len(invalid)}")
    if not invalid.empty:
        print("Primeros problemas:")
        print(invalid[["Sku", "Titulo", "Validacion"]].head(15).to_string(index=False))


if __name__ == "__main__":
    main()
