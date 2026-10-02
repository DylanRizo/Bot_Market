from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request


def fail(message: str) -> None:
    print(message, file=sys.stderr)
    raise SystemExit(1)


def main() -> None:
    raw_payload = sys.stdin.read().strip()
    if not raw_payload:
        fail("No recibi JSON por stdin.")

    try:
        payload = json.loads(raw_payload)
    except json.JSONDecodeError as exc:
        fail(f"JSON invalido: {exc}")

    api_key = os.getenv("FIRUPOST_AI_API_KEY") or os.getenv("OPENAI_API_KEY")
    model = os.getenv("FIRUPOST_AI_MODEL")
    base_url = os.getenv("FIRUPOST_AI_BASE_URL", "https://api.openai.com/v1").rstrip("/")
    timeout = int(os.getenv("FIRUPOST_AI_TIMEOUT", "45"))

    if not api_key:
        fail("Falta FIRUPOST_AI_API_KEY u OPENAI_API_KEY.")
    if not model:
        fail("Falta FIRUPOST_AI_MODEL.")

    product = payload.get("product", {})
    prompt = {
        "instruccion": payload.get("instruction", ""),
        "producto": product,
        "reglas": [
            "Escribe en espanol natural para Facebook Marketplace.",
            "No inventes caracteristicas que no esten en los datos.",
            "Maximo 7 lineas.",
            "Incluye precio, ubicacion y un llamado claro a escribir para confirmar talla/color.",
            "No incluyas SKU, codigos internos ni cantidades exactas de stock.",
            "No uses emojis ni hashtags.",
        ],
    }

    request_payload = {
        "model": model,
        "messages": [
            {
                "role": "system",
                "content": "Eres un redactor de publicaciones de Facebook Marketplace para una tienda.",
            },
            {
                "role": "user",
                "content": json.dumps(prompt, ensure_ascii=False),
            },
        ],
        "temperature": 0.4,
        "max_tokens": 220,
    }

    request = urllib.request.Request(
        f"{base_url}/chat/completions",
        data=json.dumps(request_payload, ensure_ascii=False).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        method="POST",
    )

    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            response_payload = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        fail(f"HTTP {exc.code}: {body}")
    except Exception as exc:  # noqa: BLE001 - CLI wrapper reports provider errors.
        fail(f"No se pudo llamar la IA: {exc}")

    try:
        description = response_payload["choices"][0]["message"]["content"].strip()
    except (KeyError, IndexError, TypeError) as exc:
        fail(f"Respuesta IA inesperada: {exc}; payload={response_payload}")

    if len(description) < 20:
        fail("La descripcion generada fue demasiado corta.")

    print(description)


if __name__ == "__main__":
    main()
