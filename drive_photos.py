"""Biblioteca de fotos de producto en Google Drive, en solo lectura.

Sigue la convencion MKT-GUI-001 del Departamento de Marketing:
``CODIGO-COLOR - Nombre descriptivo - Numero.ext``, con la talla omitida. Una
foto ``CMP-NEG`` sirve para ``CMP-NEG-S``, ``CMP-NEG-M`` y ``CMP-NEG-L``.

Se busca en toda la carpeta configurada. Las fotografias de producto van primero
y despues flyers, publicidad e infografias con el mismo codigo, para que la
portada del anuncio muestre el producto. Las tomas de inventario y de empaque
quedan fuera: muestran varios productos o no lo muestran como se vende.
"""
from __future__ import annotations

import io
import re
import unicodedata
import webbrowser
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterable


SCRATCH_DIR = Path(__file__).resolve().parent
CREDENTIALS_PATH = SCRATCH_DIR / "credenciales_google.json"
TOKEN_PATH = SCRATCH_DIR / "token_google_drive.json"
CACHE_DIR = SCRATCH_DIR / "sgi_drive_cache"
SCOPES = ["https://www.googleapis.com/auth/drive.readonly"]
FOLDER_MIME = "application/vnd.google-apps.folder"
MAX_PHOTOS_PER_GROUP = 10

TITLE_PATTERN = re.compile(
    r"^(?P<code>[A-Za-z0-9]+(?:-[A-Za-z0-9]+)+) - (?P<description>.+?) - (?P<number>\d{1,3})\.(?P<extension>[A-Za-z0-9]+)$"
)
FOLDER_ID_PATTERN = re.compile(r"^[A-Za-z0-9_-]{10,}$")
SIZE_TOKENS = {"XS", "S", "M", "L", "XL", "2XL", "XXL", "3XL", "U", "UNI", "X"}
EXCLUDED_DESCRIPTION_WORDS = ("inventario", "empaque")
PROMOTIONAL_DESCRIPTION_WORDS = ("publicidad", "infografia", "flyer")
EXCLUDED_CODES = {"MKT-MUL"}
IMAGE_EXTENSIONS = {"jpg", "jpeg", "png", "webp", "heic", "heif"}
HEIF_EXTENSIONS = {"heic", "heif"}
DEFAULT_EXCLUDED_FOLDERS: tuple[str, ...] = ()


class DriveNotAuthorized(RuntimeError):
    """Falta el consentimiento de solo lectura de Google Drive."""


def plain_text(text: str) -> str:
    normalized = unicodedata.normalize("NFKD", str(text))
    return normalized.encode("ascii", "ignore").decode("ascii").lower().strip()


def product_group(code: str) -> str:
    """``CMP-NEG-M`` y ``CMP-NEG`` son el mismo grupo. Algunas fotos si llevan la
    talla unica (``DUR-NEG-U``), asi que se quita tambien de sus nombres."""
    parts = str(code).strip().upper().split("-")
    if len(parts) >= 3 and parts[-1] in SIZE_TOKENS:
        parts = parts[:-1]
    return "-".join(parts)


@dataclass(frozen=True)
class DrivePhoto:
    file_id: str
    title: str
    group: str
    number: int
    extension: str
    md5: str
    promotional: bool = False


def parse_photo(file: dict[str, Any]) -> DrivePhoto | None:
    title = str(file.get("name") or "").strip()
    match = TITLE_PATTERN.match(title)
    if not match:
        return None
    code = match["code"].upper()
    extension = match["extension"].lower()
    if code in EXCLUDED_CODES or code.endswith("-VAR") or extension not in IMAGE_EXTENSIONS:
        return None
    description = plain_text(match["description"])
    if any(word in description for word in EXCLUDED_DESCRIPTION_WORDS):
        return None
    md5 = str(file.get("md5Checksum") or "")
    if not md5 or not file.get("id"):
        return None
    return DrivePhoto(
        file_id=str(file["id"]),
        title=title,
        group=product_group(code),
        number=int(match["number"]),
        extension=extension,
        md5=md5,
        promotional=any(word in description for word in PROMOTIONAL_DESCRIPTION_WORDS),
    )


def group_photos(files: Iterable[dict[str, Any]]) -> dict[str, list[DrivePhoto]]:
    """Agrupa por ``FAMILIA-COLOR``, en orden de numero.

    Las mismas fotos estan copiadas en varias carpetas (por ejemplo en "Pagina
    web"), a veces con otro nombre. Se descartan por contenido (``md5``) para no
    publicar la misma imagen dos veces en un anuncio. La publicidad va despues
    de las fotos del producto, asi nunca queda como portada si hay una foto real.
    """
    parsed = [photo for photo in (parse_photo(file) for file in files) if photo]
    parsed.sort(key=lambda photo: (photo.group, photo.promotional, photo.number, photo.title))
    seen: set[str] = set()
    groups: dict[str, list[DrivePhoto]] = {}
    for photo in parsed:
        if photo.md5 in seen:
            continue
        seen.add(photo.md5)
        bucket = groups.setdefault(photo.group, [])
        if len(bucket) < MAX_PHOTOS_PER_GROUP:
            bucket.append(photo)
    return groups


def convert_heif_to_jpeg(data: bytes) -> bytes:
    import pillow_heif
    from PIL import Image, ImageOps

    pillow_heif.register_heif_opener()
    with Image.open(io.BytesIO(data)) as image:
        rgb = ImageOps.exif_transpose(image).convert("RGB")
        output = io.BytesIO()
        rgb.save(output, format="JPEG", quality=90)
        return output.getvalue()


CAPTURE_BROWSER_NAME = "marketplace-bot-captura"


class _CaptureBrowser(webbrowser.BaseBrowser):
    """Navegador falso: entrega el enlace de Google a quien lo vaya a abrir."""

    def __init__(self, deliver: Callable[[str], None]) -> None:
        super().__init__(CAPTURE_BROWSER_NAME)
        self._deliver = deliver

    def open(self, url: str, new: int = 0, autoraise: bool = True) -> bool:
        self._deliver(url)
        return True


class DriveLibrary:
    def __init__(self, service: Any, cache_dir: Path = CACHE_DIR) -> None:
        self._service = service
        self._cache_dir = cache_dir

    @classmethod
    def connect(
        cls,
        credentials_path: Path = CREDENTIALS_PATH,
        token_path: Path = TOKEN_PATH,
        *,
        interactive: bool = False,
        cache_dir: Path = CACHE_DIR,
        open_url: Callable[[str], None] | None = None,
        timeout_seconds: int | None = None,
    ) -> DriveLibrary:
        """Conecta con Drive; si falta el permiso y es interactivo, pide el consentimiento.

        ``open_url`` recibe el enlace de Google en lugar de abrir un navegador: el
        panel corre como proceso oculto y desde ahi el navegador no siempre abre.
        """
        from google.auth.transport.requests import Request
        from google.oauth2.credentials import Credentials
        from googleapiclient.discovery import build

        credentials = None
        if token_path.is_file():
            try:
                credentials = Credentials.from_authorized_user_file(str(token_path), SCOPES)
            except (ValueError, OSError):
                credentials = None
        if credentials and not credentials.valid and credentials.expired and credentials.refresh_token:
            try:
                credentials.refresh(Request())
            except Exception:
                credentials = None
        if not credentials or not credentials.valid:
            # El trabajador corre desatendido: nunca abre un navegador por su cuenta.
            if not interactive:
                raise DriveNotAuthorized("Falta autorizar el acceso de solo lectura a Google Drive desde el panel.")
            from google_auth_oauthlib.flow import InstalledAppFlow

            flow = InstalledAppFlow.from_client_secrets_file(str(credentials_path), scopes=SCOPES)
            browser = None
            if open_url is not None:
                webbrowser.register(CAPTURE_BROWSER_NAME, None, _CaptureBrowser(open_url))
                browser = CAPTURE_BROWSER_NAME
            credentials = flow.run_local_server(port=0, browser=browser, timeout_seconds=timeout_seconds)
        token_path.write_text(credentials.to_json(), encoding="utf-8")
        return cls(build("drive", "v3", credentials=credentials, cache_discovery=False), cache_dir)

    def list_image_files(
        self,
        root_folder_ids: Iterable[str],
        excluded_folder_names: Iterable[str] = DEFAULT_EXCLUDED_FOLDERS,
    ) -> list[dict[str, Any]]:
        excluded = {plain_text(name) for name in excluded_folder_names}
        pending = [folder for folder in root_folder_ids if FOLDER_ID_PATTERN.fullmatch(str(folder))]
        visited: set[str] = set()
        files: list[dict[str, Any]] = []
        while pending:
            folder_id = pending.pop()
            if folder_id in visited:
                continue
            visited.add(folder_id)
            page_token = None
            while True:
                response = (
                    self._service.files()
                    .list(
                        q=f"'{folder_id}' in parents and trashed = false",
                        fields="nextPageToken, files(id, name, mimeType, md5Checksum)",
                        pageSize=1000,
                        pageToken=page_token,
                        supportsAllDrives=True,
                        includeItemsFromAllDrives=True,
                    )
                    .execute()
                )
                for item in response.get("files", []):
                    mime = str(item.get("mimeType") or "")
                    if mime == FOLDER_MIME:
                        child = str(item.get("id") or "")
                        if plain_text(item.get("name") or "") not in excluded and FOLDER_ID_PATTERN.fullmatch(child):
                            pending.append(child)
                    elif mime.startswith("image/"):
                        files.append(item)
                page_token = response.get("nextPageToken")
                if not page_token:
                    break
        return files

    def download(self, photo: DrivePhoto) -> Path:
        """Descarga una sola vez por contenido. HEIC se convierte a JPG, que es lo
        que Marketplace acepta de forma fiable."""
        target_extension = "jpg" if photo.extension in HEIF_EXTENSIONS | {"jpeg"} else photo.extension
        target = self._cache_dir / f"{photo.md5}.{target_extension}"
        if target.is_file() and target.stat().st_size > 0:
            return target
        from googleapiclient.http import MediaIoBaseDownload

        buffer = io.BytesIO()
        downloader = MediaIoBaseDownload(buffer, self._service.files().get_media(fileId=photo.file_id))
        done = False
        while not done:
            _, done = downloader.next_chunk()
        data = buffer.getvalue()
        if photo.extension in HEIF_EXTENSIONS:
            data = convert_heif_to_jpeg(data)
        self._cache_dir.mkdir(parents=True, exist_ok=True)
        temporary = target.with_name(target.name + ".tmp")
        temporary.write_bytes(data)
        temporary.replace(target)
        return target
