from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any


DEFAULT_MODEL = os.environ.get("FIRUPOST_OPENAI_MODEL", "gpt-5.6-luna")
DEFAULT_TONE = "directo, cercano y profesional"
SECRET_PATH = Path(__file__).resolve().parent / ".firupost_openai_key.bin"

DEVELOPER_PROMPT = """Escribe una descripcion de venta para Facebook Marketplace en espanol de Nicaragua.
Usa entre 70 y 120 palabras, texto sencillo y un tono directo, cercano y profesional.
Conserva exactamente los datos proporcionados. No inventes marcas, materiales, beneficios, medidas ni condiciones.
No menciones SKU, codigos internos, cantidades de stock, automatizacion ni inteligencia artificial.
Incluye precio, variantes disponibles, ubicacion y una llamada a escribir para coordinar la entrega.
No uses emojis, hashtags, mayusculas sostenidas ni titulos decorativos."""

TAGS_DEVELOPER_PROMPT = """Genera etiquetas de producto para el campo "Product tags" de Facebook Marketplace.
Devuelve solamente entre 4 y 8 etiquetas separadas por comas, sin hashtags ni explicaciones.
Cada etiqueta debe ser corta, util para encontrar el producto y estar basada solo en los datos recibidos.
No inventes marcas, materiales, beneficios, medidas ni condiciones. No incluyas SKU, stock, precio ni ubicacion."""


def load_api_key() -> str:
    environment_key = os.environ.get("OPENAI_API_KEY", "").strip()
    if environment_key:
        return environment_key
    if not SECRET_PATH.is_file():
        return ""
    try:
        import win32crypt

        encrypted = SECRET_PATH.read_bytes()
        return win32crypt.CryptUnprotectData(encrypted, None, None, None, 0)[1].decode("utf-8").strip()
    except Exception:
        return ""


def save_api_key(api_key: str) -> None:
    key = str(api_key or "").strip()
    if len(key) < 20:
        raise ValueError("La clave de OpenAI no parece valida.")
    try:
        import win32crypt

        encrypted = win32crypt.CryptProtectData(
            key.encode("utf-8"),
            "FiruPost OpenAI API key",
            None,
            None,
            None,
            0,
        )
        SECRET_PATH.write_bytes(encrypted)
        os.environ["OPENAI_API_KEY"] = key
    except ImportError as exc:
        raise RuntimeError("En este equipo configura OPENAI_API_KEY como variable de entorno.") from exc


def ai_status() -> dict[str, Any]:
    configured = bool(load_api_key())
    return {
        "configured": configured,
        "model": DEFAULT_MODEL,
        "mode": "openai" if configured else "local",
        "message": "IA lista" if configured else "Redaccion local; falta OPENAI_API_KEY",
    }


def sanitize_description(text: str, fallback: str) -> str:
    cleaned_lines: list[str] = []
    for raw_line in str(text or "").splitlines():
        line = raw_line.strip()
        if not line:
            if cleaned_lines and cleaned_lines[-1]:
                cleaned_lines.append("")
            continue
        lower = line.lower()
        if re.search(r"\bsku\b|\bstock\b|existencias?\s*:\s*\d+", lower):
            continue
        cleaned_lines.append(line)
    cleaned = "\n".join(cleaned_lines).strip()
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned)
    return cleaned[:1200] if cleaned else fallback


def sanitize_tags(value: Any, fallback: list[str] | None = None) -> list[str]:
    raw_values = value if isinstance(value, list) else [value]
    # Facebook no acepta comas dentro de una etiqueta: "S, M, L" se rechazaba entera.
    pieces = [piece for raw in raw_values for piece in re.split(r"[,;|\n]", str(raw or ""))]
    tags: list[str] = []
    seen: set[str] = set()
    for raw in pieces:
        tag = re.sub(r"\s+", " ", str(raw or "").replace("#", "").strip(" .,-"))
        lowered = tag.lower()
        # Las tallas sueltas (S, M, XL) no son terminos de busqueda utiles.
        if len(tag) < 3 or len(tag) > 48 or lowered in seen:
            continue
        if re.search(r"\bsku\b|\bstock\b|\bexistencias?\b|\bc\$|\bnio\b", lowered):
            continue
        tags.append(tag)
        seen.add(lowered)
        if len(tags) == 8:
            break
    return tags or list(fallback or [])[:8]


def local_product_tags(facts: dict[str, Any]) -> list[str]:
    title = str(facts.get("titulo") or "").strip()
    lowered = title.lower()
    # Solo el nombre del producto: los colores se agregan aparte.
    values: list[str] = [re.split(r" - | \| | en (?=[A-Z])", title)[0].strip() or title]
    has_product_type = False
    if "compres" in lowered:
        has_product_type = True
        values.extend(["camisa de compresion", "ropa deportiva", "gimnasio", "entrenamiento"])
    elif "short" in lowered:
        has_product_type = True
        values.extend(["shorts deportivos", "ropa deportiva", "gimnasio", "entrenamiento"])
    elif "enterizo" in lowered:
        has_product_type = True
        values.extend(["enterizo deportivo", "outfit deportivo", "gimnasio", "entrenamiento"])
    elif "leggin" in lowered:
        has_product_type = True
        values.extend(["leggins deportivos", "ropa deportiva", "gimnasio", "entrenamiento"])
    elif "bolso" in lowered:
        has_product_type = True
        values.extend(["bolso deportivo", "bolso de gimnasio", "accesorios deportivos", "entrenamiento"])
    elif "durag" in lowered:
        has_product_type = True
        values.extend(["durag", "accesorios de cabello", "estilo urbano", "uso diario"])
    elif "muñequera" in lowered or "munequera" in lowered:
        has_product_type = True
        values.extend(["muñequeras deportivas", "accesorios de gimnasio", "entrenamiento", "fitness"])
    elif "strap" in lowered:
        has_product_type = True
        values.extend(["straps para gimnasio", "accesorios de gimnasio", "entrenamiento", "fitness"])
    else:
        values.extend(["ropa deportiva", "gimnasio", "entrenamiento"])
    keys = ("color", "colores", "estilo")
    if not has_product_type:
        keys = (*keys, "uso")
    for key in keys:
        value = facts.get(key)
        if isinstance(value, list):
            values.extend(str(item) for item in value)
        elif value:
            values.append(str(value))
    return sanitize_tags(values)


def generate_product_tags(
    facts: dict[str, Any],
    *,
    enabled: bool = True,
    model: str | None = None,
) -> tuple[list[str], str]:
    fallback = local_product_tags(facts)
    if not enabled:
        return fallback, "local-disabled"
    api_key = load_api_key()
    if not api_key:
        return fallback, "local-no-key"
    try:
        from openai import OpenAI

        response = OpenAI(api_key=api_key).responses.create(
            model=model or DEFAULT_MODEL,
            reasoning={"effort": "low"},
            text={"verbosity": "low"},
            max_output_tokens=120,
            store=False,
            input=[
                {"role": "developer", "content": TAGS_DEVELOPER_PROMPT},
                {"role": "user", "content": json.dumps({"datos_del_producto": facts}, ensure_ascii=False)},
            ],
        )
        return sanitize_tags(response.output_text, fallback), f"openai:{model or DEFAULT_MODEL}"
    except Exception as exc:
        return fallback, f"local-error:{type(exc).__name__}"


def generate_sales_description(
    facts: dict[str, Any],
    fallback: str,
    *,
    enabled: bool = True,
    model: str | None = None,
    tone: str = DEFAULT_TONE,
) -> tuple[str, str]:
    if not enabled:
        return sanitize_description(fallback, fallback), "local-disabled"
    api_key = load_api_key()
    if not api_key:
        return sanitize_description(fallback, fallback), "local-no-key"

    try:
        from openai import OpenAI

        client = OpenAI(api_key=api_key)
        response = client.responses.create(
            model=model or DEFAULT_MODEL,
            reasoning={"effort": "low"},
            text={"verbosity": "low"},
            max_output_tokens=350,
            store=False,
            input=[
                {
                    "role": "developer",
                    "content": DEVELOPER_PROMPT,
                },
                {
                    "role": "user",
                    "content": json.dumps(
                        {
                            "tono": tone,
                            "datos_del_producto": facts,
                        },
                        ensure_ascii=False,
                    ),
                },
            ],
        )
        description = sanitize_description(response.output_text, fallback)
        return description, f"openai:{model or DEFAULT_MODEL}"
    except Exception as exc:
        return sanitize_description(fallback, fallback), f"local-error:{type(exc).__name__}"
