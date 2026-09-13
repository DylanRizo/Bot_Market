"""Cliente de solo lectura del catalogo del SGI La Comarca.

Consume ``GET /api/v1/integrations/catalog`` con una llave de integracion
(ADR-017 del SGI). El bot nunca escribe en el SGI y nunca recibe costos.

La llave se guarda cifrada con DPAPI y no aparece en logs, reportes ni en la
representacion del cliente.
"""
from __future__ import annotations

import json
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable


SCRATCH_DIR = Path(__file__).resolve().parent
SECRET_PATH = SCRATCH_DIR / ".sgi_integration_key.bin"
SETTINGS_PATH = SCRATCH_DIR / "marketplace_integrations.json"
KEY_PATTERN = re.compile(r"^[A-Za-z0-9_-]{43}$")
PAGE_SIZE = 100
LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1"}

PRICE_ISSUE_LABELS = {
    "MIXED": "Precios distintos entre bodegas en el SGI",
    "REVIEW": "Precio en revision en el SGI",
    "MISSING": "Falta el precio en el SGI",
}


class SgiError(RuntimeError):
    """Fallo al consultar el SGI. ``code`` es estable para alertas y pruebas."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class CatalogItem:
    code: str
    name: str
    description: str
    total_quantity: float
    unit_price: float | None
    price_issue: str | None

    @property
    def publishable(self) -> bool:
        return self.total_quantity > 0 and self.unit_price is not None and self.price_issue is None


def load_settings(path: Path = SETTINGS_PATH) -> dict[str, Any]:
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def load_integration_key(secret_path: Path = SECRET_PATH) -> str:
    environment_key = os.environ.get("SGI_INTEGRATION_KEY", "").strip()
    if environment_key:
        return environment_key
    if not secret_path.is_file():
        return ""
    try:
        import win32crypt

        return win32crypt.CryptUnprotectData(secret_path.read_bytes(), None, None, None, 0)[1].decode("utf-8").strip()
    except Exception:
        return ""


def save_integration_key(key: str, secret_path: Path = SECRET_PATH) -> None:
    value = str(key or "").strip()
    if not KEY_PATTERN.fullmatch(value):
        raise ValueError("La llave del SGI debe tener 43 caracteres: letras, numeros, guion y guion bajo.")
    try:
        import win32crypt
    except ImportError as exc:
        raise RuntimeError("En este equipo configura SGI_INTEGRATION_KEY como variable de entorno.") from exc
    encrypted = win32crypt.CryptProtectData(
        value.encode("utf-8"),
        "Marketplace bot SGI integration key",
        None,
        None,
        None,
        0,
    )
    secret_path.write_bytes(encrypted)


def integration_key_configured(secret_path: Path = SECRET_PATH) -> bool:
    return bool(KEY_PATTERN.fullmatch(load_integration_key(secret_path)))


def validate_base_url(base_url: str) -> str:
    parsed = urllib.parse.urlparse(str(base_url or "").strip())
    if parsed.scheme not in {"https", "http"} or not parsed.hostname:
        raise SgiError(
            "NOT_CONFIGURED",
            "Configura la direccion del SGI, por ejemplo https://api-sgi.example.com.",
        )
    # La llave viaja en cada consulta: fuera de esta computadora solo por HTTPS.
    if parsed.scheme == "http" and parsed.hostname not in LOCAL_HOSTS:
        raise SgiError("NOT_CONFIGURED", "El SGI debe usar HTTPS fuera de esta computadora.")
    return f"{parsed.scheme}://{parsed.netloc}"


def _to_float(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def parse_catalog_item(raw: Any) -> CatalogItem:
    if not isinstance(raw, dict) or not str(raw.get("code") or "").strip():
        raise SgiError("INVALID_RESPONSE", "El SGI devolvio un producto sin codigo.")
    issue = str(raw["priceIssue"]) if raw.get("priceIssue") else None
    price = None if issue else _to_float(raw.get("unitPrice"))
    if price is not None and price <= 0:
        price = None
    if price is None and issue is None:
        issue = "MISSING"
    return CatalogItem(
        code=str(raw["code"]).strip().upper(),
        name=str(raw.get("name") or "").strip(),
        description=str(raw.get("description") or "").strip(),
        total_quantity=_to_float(raw.get("totalQuantity")) or 0.0,
        unit_price=price,
        price_issue=issue,
    )


class SgiClient:
    def __init__(
        self,
        base_url: str,
        key: str,
        *,
        opener: Callable[..., Any] = urllib.request.urlopen,
        sleep: Callable[[float], None] = time.sleep,
        max_retries: int = 3,
        timeout: float = 20.0,
    ) -> None:
        self.base_url = validate_base_url(base_url)
        if not KEY_PATTERN.fullmatch(str(key or "")):
            raise SgiError("NOT_CONFIGURED", "Falta la llave de integracion del SGI o no tiene el formato correcto.")
        self._key = key
        self._opener = opener
        self._sleep = sleep
        self._max_retries = max(0, max_retries)
        self._timeout = timeout

    @classmethod
    def from_local_configuration(cls, **kwargs: Any) -> SgiClient:
        settings = load_settings().get("sgi") or {}
        return cls(str(settings.get("base_url") or ""), load_integration_key(), **kwargs)

    def __repr__(self) -> str:
        return f"SgiClient(base_url={self.base_url!r}, key=[REDACTED])"

    def catalog(self) -> list[CatalogItem]:
        items: list[CatalogItem] = []
        page = 1
        while True:
            data = self._get(f"/api/v1/integrations/catalog?page={page}&pageSize={PAGE_SIZE}")
            batch = data.get("items") if isinstance(data, dict) else None
            pagination = data.get("pagination") if isinstance(data, dict) else None
            if not isinstance(batch, list) or not isinstance(pagination, dict):
                raise SgiError("INVALID_RESPONSE", "El SGI devolvio un catalogo con formato inesperado.")
            items.extend(parse_catalog_item(raw) for raw in batch)
            if not batch or page >= int(pagination.get("totalPages") or 0):
                return items
            page += 1

    def _get(self, path: str) -> Any:
        url = self.base_url + path
        last_error: SgiError | None = None
        for attempt in range(self._max_retries + 1):
            request = urllib.request.Request(
                url,
                headers={
                    "Accept": "application/json",
                    "Authorization": f"Bearer {self._key}",
                    "User-Agent": "la-comarca-marketplace-bot",
                },
            )
            try:
                with self._opener(request, timeout=self._timeout) as response:
                    payload = json.loads(response.read().decode("utf-8"))
                return payload.get("data") if isinstance(payload, dict) else None
            # `from None` evita encadenar la peticion, que lleva la llave, en la traza.
            except urllib.error.HTTPError as exc:
                if exc.code == 401:
                    raise SgiError(
                        "KEY_REJECTED",
                        "El SGI rechazo la llave: esta revocada o caducada, o su dueno cambio la contrasena.",
                    ) from None
                if exc.code == 403:
                    raise SgiError("FORBIDDEN", "La cuenta duena de la llave ya no puede consultar el inventario.") from None
                if exc.code == 503:
                    raise SgiError("DISABLED", "La integracion esta deshabilitada en el SGI.") from None
                if exc.code == 429:
                    last_error = SgiError("RATE_LIMITED", "El SGI limito las consultas por minuto.")
                elif exc.code >= 500:
                    last_error = SgiError("UNAVAILABLE", f"El SGI respondio con error {exc.code}.")
                else:
                    raise SgiError("INVALID_RESPONSE", f"El SGI respondio {exc.code}.") from None
            except (urllib.error.URLError, TimeoutError, OSError, ValueError) as exc:
                last_error = SgiError("UNAVAILABLE", f"No se pudo contactar al SGI ({exc.__class__.__name__}).")
            if attempt < self._max_retries:
                self._sleep(min(30.0, 2.0 * (2**attempt)))
        raise last_error or SgiError("UNAVAILABLE", "No se pudo contactar al SGI.")
