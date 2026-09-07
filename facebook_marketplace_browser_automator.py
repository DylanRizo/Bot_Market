from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable

import pandas as pd
from selenium import webdriver
from selenium.common.exceptions import WebDriverException
from selenium.webdriver import ChromeOptions
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait


SCRATCH_DIR = Path(__file__).resolve().parent
DEFAULT_EXCEL = SCRATCH_DIR / "firupost_test_batch" / "ArticulosGenerados_TEST.xlsx"
DEFAULT_IMAGES = SCRATCH_DIR / "firupost_test_batch" / "imagenes"
DEFAULT_PROFILE = Path.home() / "AppData" / "Local" / "BotInvisibleChrome"
CREATE_URL = "https://www.facebook.com/marketplace/create/item"
FAILURES_DIR = Path(__file__).resolve().parent / "marketplace_failures"


class PublishOutcomeUnknown(RuntimeError):
    pass


IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp"}
LIST_SEPARATORS = (";", "|")


if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")


def log(message: str) -> None:
    print(f"[marketplace-browser] {message}", flush=True)


def error_code(exc: Exception) -> str:
    text = str(exc).lower()
    if isinstance(exc, PublishOutcomeUnknown):
        return "PUBLISH_OUTCOME_UNKNOWN"
    if any(value in text for value in ("sesion", "log in", "captcha", "2fa", "revision")):
        return "SESSION_BLOCKED"
    if any(value in text for value in ("imagen", "foto", "photo")):
        return "IMAGE_INVALID"
    if any(value in text for value in ("category", "categoria", "condition", "campo", "next")):
        return "FORM_FIELD"
    return "INTERNAL"


def capture_failure(driver, stage: str, exc: Exception) -> Path | None:
    if driver is None:
        return None
    FAILURES_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    base = FAILURES_DIR / f"failure-{stamp}-{os.getpid()}"
    try:
        driver.save_screenshot(str(base.with_suffix(".png")))
        payload = {
            "created_at": datetime.now().isoformat(timespec="seconds"),
            "stage": stage,
            "error_code": error_code(exc),
            "error": str(exc),
            "url": driver.current_url,
            "title": driver.title,
        }
        base.with_suffix(".json").write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        return base.with_suffix(".png")
    except Exception:
        return None


def verify_publish_result(driver: webdriver.Chrome, timeout: int = 30) -> None:
    confirmations = (
        "your listing is now published",
        "your listing has been published",
        "listing published",
        "tu publicacion se publico",
        "publicacion publicada",
    )

    def published(current_driver):
        url = current_driver.current_url.lower()
        if "/marketplace/create/item" not in url:
            return True
        body = current_driver.find_element(By.TAG_NAME, "body").text.lower()
        return any(text in body for text in confirmations)

    try:
        WebDriverWait(driver, timeout).until(published)
    except Exception as exc:
        raise PublishOutcomeUnknown(
            "Facebook recibio el clic en Publish, pero no pude confirmar el resultado. "
            "No se debe reintentar automaticamente hasta revisar Marketplace."
        ) from exc


def text_value(value) -> str:
    try:
        if pd.isna(value):
            return ""
    except TypeError:
        pass
    return "" if value is None else str(value).strip()


def split_list_value(value: str) -> list[str]:
    text = text_value(value)
    if not text:
        return []
    for separator in LIST_SEPARATORS:
        if separator in text:
            return [part.strip() for part in text.split(separator) if part.strip()]
    return [text]


def sanitize_public_description(description: str) -> str:
    cleaned_lines: list[str] = []
    for raw_line in text_value(description).splitlines():
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


def resolve_image_paths(images_root: Path, folder_value: str, image_name_value: str) -> list[Path]:
    folders = split_list_value(folder_value)
    image_names = split_list_value(image_name_value)
    if not image_names and folders:
        image_names = ["foto_1"] * len(folders)

    images: list[Path] = []
    for index, image_name in enumerate(image_names):
        image_path = Path(image_name)
        if image_path.is_absolute() and image_path.exists():
            images.append(image_path)
            continue

        folder = folders[index] if index < len(folders) else (folders[0] if folders else "")
        search_dirs = [images_root / folder] if folder else []
        search_dirs.append(images_root)

        found = None
        for search_dir in search_dirs:
            exact = search_dir / image_name
            if exact.exists():
                found = exact
                break
            if Path(image_name).suffix.lower() not in IMAGE_EXTENSIONS:
                for ext in IMAGE_EXTENSIONS:
                    candidate = search_dir / f"{image_name}{ext}"
                    if candidate.exists():
                        found = candidate
                        break
            if found:
                break
        if found:
            images.append(found)

    if not images:
        raise FileNotFoundError(f"No encontre imagen {image_name_value} en {images_root}")
    return images


def read_product(excel_path: Path, images_root: Path, row_index: int) -> dict[str, Any]:
    df = pd.read_excel(excel_path)
    if df.empty:
        raise ValueError(f"El Excel esta vacio: {excel_path}")
    if row_index < 0 or row_index >= len(df):
        raise ValueError(f"row-index fuera de rango: {row_index}; filas={len(df)}")

    row = df.iloc[row_index]
    images = resolve_image_paths(
        images_root,
        text_value(row.get("CarpetaImg")),
        text_value(row.get("NombreImg")),
    )

    return {
        "title": text_value(row.get("Titulo")),
        "price": text_value(row.get("Precio")),
        "category": text_value(row.get("Categoria")) or "Apparel",
        "condition": text_value(row.get("Condicion")) or "New",
        "description": sanitize_public_description(row.get("Descripcion")),
        "tags": split_list_value(row.get("Etiqueta")),
        "sku": text_value(row.get("Sku")),
        "location": text_value(row.get("Ubicacion")),
        "image": str(images[0].resolve()),
        "images": [str(image.resolve()) for image in images],
    }


def chrome_driver(args: argparse.Namespace) -> webdriver.Chrome:
    options = ChromeOptions()
    if args.debugger_address:
        options.add_experimental_option("debuggerAddress", args.debugger_address)
        return webdriver.Chrome(options=options)

    options.add_argument(f"--user-data-dir={args.chrome_profile}")
    options.add_argument("--profile-directory=Default")
    options.add_argument("--lang=en-US")
    if args.start_maximized:
        options.add_argument("--start-maximized")

    try:
        return webdriver.Chrome(service=Service(), options=options)
    except WebDriverException as exc:
        raise RuntimeError(
            "No pude abrir Chrome con Selenium. Si ya tienes Chrome abierto con "
            "BotInvisibleChrome, cierralo o abre la sesion con "
            "abrir_sesion_facebook_firupost.ps1 actualizado."
        ) from exc


def wait_visible(driver: webdriver.Chrome, xpath: str, timeout: int = 20):
    return WebDriverWait(driver, timeout).until(EC.visibility_of_element_located((By.XPATH, xpath)))


def click_xpath(driver: webdriver.Chrome, xpath: str, timeout: int = 20) -> None:
    element = WebDriverWait(driver, timeout).until(EC.presence_of_element_located((By.XPATH, xpath)))
    driver.execute_script("arguments[0].scrollIntoView({block: 'center'});", element)
    time.sleep(0.5)
    element = WebDriverWait(driver, timeout).until(EC.element_to_be_clickable((By.XPATH, xpath)))
    try:
        element.click()
    except Exception:
        driver.execute_script("arguments[0].click();", element)


def open_combobox(driver: webdriver.Chrome, label: str, timeout: int = 20) -> bool:
    xpath = (
        f"//label[@role='combobox' and (.//*[normalize-space()={xpath_literal(label)}] or "
        f"contains(normalize-space(.), {xpath_literal(label)}))]"
    )
    try:
        element = WebDriverWait(driver, timeout).until(EC.presence_of_element_located((By.XPATH, xpath)))
    except Exception:
        return False

    driver.execute_script("arguments[0].scrollIntoView({block: 'center'});", element)
    time.sleep(0.5)
    driver.execute_script(
        """
        const el = arguments[0];
        const r = el.getBoundingClientRect();
        const x = r.right - 18;
        const y = r.top + r.height / 2;
        const target = document.elementFromPoint(x, y) || el;
        for (const type of ['pointerdown', 'mousedown', 'pointerup', 'mouseup', 'click']) {
            target.dispatchEvent(new MouseEvent(type, {
                bubbles: true,
                cancelable: true,
                view: window,
                clientX: x,
                clientY: y,
            }));
        }
        """,
        element,
    )
    time.sleep(1)
    return True


def click_button(driver: webdriver.Chrome, label: str, timeout: int = 20) -> bool:
    end = time.time() + timeout
    while time.time() < end:
        clicked = driver.execute_script(
            """
            const label = arguments[0];
            const candidates = [...document.querySelectorAll('[role="button"], button')]
                .map(el => {
                    const rect = el.getBoundingClientRect();
                    const text = (el.innerText || el.getAttribute('aria-label') || '').trim();
                    return {
                        el,
                        text,
                        disabled: el.getAttribute('aria-disabled') || el.disabled,
                        x: rect.x,
                        y: rect.y,
                        w: rect.width,
                        h: rect.height,
                    };
                })
                .filter(o => o.text === label && !o.disabled
                    && o.w > 60 && o.h > 20
                    && o.x >= -5 && o.x < innerWidth
                    && o.y >= -5 && o.y < innerHeight)
                .sort((a, b) => (b.w * b.h) - (a.w * a.h));
            const button = candidates[0];
            if (!button) return false;
            const x = button.x + button.w / 2;
            const y = button.y + button.h / 2;
            const target = document.elementFromPoint(x, y) || button.el;
            for (const type of ['pointerdown', 'mousedown', 'pointerup', 'mouseup', 'click']) {
                target.dispatchEvent(new MouseEvent(type, {
                    bubbles: true,
                    cancelable: true,
                    view: window,
                    clientX: x,
                    clientY: y,
                }));
            }
            return true;
            """,
            label,
        )
        if clicked:
            time.sleep(1.5)
            return True
        time.sleep(0.5)
    return False


def close_floating_panels(driver: webdriver.Chrome) -> None:
    try:
        driver.switch_to.active_element.send_keys(Keys.ESCAPE)
        time.sleep(0.4)
    except Exception:
        pass


def set_value(driver: webdriver.Chrome, element, value: str, verify_exact: bool = False) -> None:
    """Set a controlled Marketplace field, allowing Facebook display formatting."""
    actual = driver.execute_script(
        """
        const el = arguments[0];
        const nextValue = String(arguments[1]);
        el.scrollIntoView({block: 'center'});
        el.focus();
        if ('value' in el) {
          const prototype = el instanceof HTMLTextAreaElement
            ? HTMLTextAreaElement.prototype
            : el instanceof HTMLInputElement
              ? HTMLInputElement.prototype
              : Object.getPrototypeOf(el);
          const setter = Object.getOwnPropertyDescriptor(prototype, 'value')?.set;
          if (setter) setter.call(el, nextValue);
          else el.value = nextValue;
        } else {
          el.textContent = nextValue;
        }
        el.dispatchEvent(new InputEvent('input', {bubbles: true, inputType: 'insertText', data: nextValue}));
        el.dispatchEvent(new Event('change', {bubbles: true}));
        el.dispatchEvent(new Event('blur', {bubbles: true}));
        return 'value' in el ? String(el.value) : String(el.textContent || '');
        """,
        element,
        value,
    )
    if verify_exact and actual != str(value):
        raise RuntimeError(f"El campo no conservo el valor esperado ({actual!r}).")


def field_by_label(driver: webdriver.Chrome, labels: Iterable[str], tag: str = "*", timeout: int = 15):
    label_predicates = " or ".join(
        [
            f".//*[normalize-space()={xpath_literal(label)}] or contains(normalize-space(.), {xpath_literal(label)})"
            for label in labels
        ]
    )
    xpath = f"//label[{label_predicates}]//{tag}[self::input or self::textarea]"
    return wait_visible(driver, xpath, timeout=timeout)


def field_by_context(driver: webdriver.Chrome, labels: Iterable[str], selector: str, timeout: int = 12):
    """Find a visible control through the short text block around it.

    Marketplace sometimes renders optional fields inside nested divs instead of
    directly beneath a label. Looking at the nearest text containers is more
    stable than relying only on the HTML label structure.
    """

    def locate(current_driver):
        return current_driver.execute_script(
            """
            const normalize = value => String(value || '')
              .normalize('NFD').replace(/[\\u0300-\\u036f]/g, '')
              .replace(/\\s+/g, ' ').trim().toLowerCase();
            const labels = arguments[0].map(normalize);
            const selector = arguments[1];
            const controls = [...document.querySelectorAll(selector)];
            for (const control of controls) {
              const rect = control.getBoundingClientRect();
              const style = window.getComputedStyle(control);
              if (rect.width < 2 || rect.height < 2 || style.visibility === 'hidden' || style.display === 'none') continue;
              const ownText = normalize(control.getAttribute('aria-label') || control.getAttribute('placeholder') || control.getAttribute('name'));
              if (labels.some(label => ownText === label || ownText.includes(label))) return control;
              let depth = 0;
              for (let node = control; node && node !== document.body && depth < 10; node = node.parentElement, depth += 1) {
                const text = normalize(node.innerText || node.textContent);
                const exact = labels.some(label => text === label || text.startsWith(`${label} `) || text.endsWith(` ${label}`));
                const shortContext = text.length <= 280 && labels.some(label => text.includes(label));
                if (exact || shortContext) return control;
              }
            }
            return null;
            """,
            list(labels),
            selector,
        )

    return WebDriverWait(driver, timeout).until(locate)


DESCRIPTION_LABELS = ["Description", "Descripcion", "Descripción"]
PRODUCT_TAG_LABELS = ["Product tags", "Product tag", "Etiquetas del producto", "Etiquetas de producto"]


def description_field(driver: webdriver.Chrome, timeout: int = 12):
    return field_by_context(driver, DESCRIPTION_LABELS, "textarea, [contenteditable='true']", timeout=timeout)


def product_tags_field(driver: webdriver.Chrome, timeout: int = 12):
    return field_by_context(driver, PRODUCT_TAG_LABELS, "textarea, input[type='text'], [contenteditable='true']", timeout=timeout)


def set_dynamic_field(driver: webdriver.Chrome, locate_field, value: str, attempts: int = 3) -> None:
    """Reacquire a field when Marketplace rerenders it after a dependent choice."""
    errors = []
    for _ in range(attempts):
        try:
            field = locate_field(driver, timeout=5)
            set_value(driver, field, value, verify_exact=True)
            return
        except Exception as exc:
            errors.append(f"{type(exc).__name__}: {exc}")
            time.sleep(0.4)
    raise RuntimeError(" | ".join(errors[-2:]))


def fill_product_tags(driver: webdriver.Chrome, tags: list[str]) -> int:
    if not tags:
        return 0
    clean_tags = [str(tag).strip() for tag in tags[:8] if str(tag).strip()]
    # Product tags is currently a textarea in Marketplace, not a token picker.
    # A single comma-separated value survives React re-renders more reliably
    # than pressing Enter after each item.
    set_dynamic_field(driver, product_tags_field, ", ".join(clean_tags))
    log(f"Product tags: {len(clean_tags)}")
    return len(clean_tags)


def xpath_literal(text: str) -> str:
    if "'" not in text:
        return f"'{text}'"
    if '"' not in text:
        return f'"{text}"'
    parts = text.split("'")
    return "concat(" + ", \"'\", ".join(f"'{part}'" for part in parts) + ")"


def upload_photo(driver: webdriver.Chrome, image_paths: list[str]) -> None:
    input_file = WebDriverWait(driver, 30).until(
        EC.presence_of_element_located((By.CSS_SELECTOR, "input[type='file']"))
    )
    if not image_paths:
        raise RuntimeError("No hay imagenes para subir.")
    input_file.send_keys("\n".join(image_paths))
    time.sleep(4 + min(len(image_paths), 10))
    log(f"Photos: {len(image_paths)}")


def ensure_marketplace_session(driver: webdriver.Chrome) -> None:
    time.sleep(5)
    close_floating_panels(driver)
    current_url = driver.current_url
    body_text = driver.find_element(By.TAG_NAME, "body").text
    login_inputs = driver.find_elements(By.CSS_SELECTOR, "input[type='email'], input[type='password']")
    if login_inputs or "Email or phone" in body_text or "Log In" in body_text:
        raise RuntimeError(
            "La sesion de Facebook no esta iniciada en el perfil BotInvisibleChrome. "
            "Ejecuta abrir_sesion_facebook_firupost.ps1, inicia sesion manualmente "
            "y vuelve a correr esta prueba."
        )
    if "/marketplace/create/item" not in current_url:
        raise RuntimeError(
            f"Facebook no abrio el formulario de creacion. URL actual: {current_url}. "
            "Revisa si Marketplace pidio login, captcha, revision o confirmacion manual."
        )


def fill_required_text_fields(driver: webdriver.Chrome, product: dict[str, str]) -> None:
    title = field_by_label(driver, ["Title"], tag="input")
    set_value(driver, title, product["title"])

    price = field_by_label(driver, ["Price"], tag="input")
    set_value(driver, price, product["price"])


def expand_more_details(driver: webdriver.Chrome) -> None:
    try:
        description_field(driver, timeout=3)
        return
    except Exception:
        pass

    try:
        click_xpath(
            driver,
            "//*[@role='button' and (.//*[normalize-space()='More details'] or contains(normalize-space(.), 'More details') or .//*[normalize-space()='Más detalles'] or contains(normalize-space(.), 'Más detalles'))]",
            timeout=5,
        )
    except Exception:
        pass


def fill_optional_details(
    driver: webdriver.Chrome,
    product: dict[str, Any],
    fill_sku: bool = True,
    strict: bool = False,
) -> None:
    log("Optional fields: dynamic lookup v2")
    expand_more_details(driver)
    errors: list[str] = []
    try:
        set_dynamic_field(driver, description_field, product["description"])
        log("Description: OK")
    except Exception as exc:
        log(f"Description: ERROR {type(exc).__name__}: {str(exc)[:180]}")
        errors.append("descripcion")

    try:
        tag_count = fill_product_tags(driver, list(product.get("tags") or []))
        if strict and tag_count == 0:
            raise RuntimeError("El producto no tiene etiquetas configuradas.")
    except Exception as exc:
        log(f"Product tags: ERROR {type(exc).__name__}: {str(exc)[:180]}")
        errors.append("etiquetas")

    if fill_sku and product["sku"]:
        try:
            sku = field_by_label(driver, ["SKU"], tag="input", timeout=5)
            set_value(driver, sku, product["sku"])
            log("SKU: OK")
        except Exception:
            log("SKU: NO ENCONTRADO")
    if strict and errors:
        raise RuntimeError(f"No se completaron campos obligatorios: {', '.join(errors)}.")


def set_checkbox(driver: webdriver.Chrome, label: str, enabled: bool = True) -> bool:
    xpath = (
        f"//*[@role='checkbox' and (.//*[normalize-space()={xpath_literal(label)}] or "
        f"contains(normalize-space(.), {xpath_literal(label)}))]"
    )
    try:
        checkbox = WebDriverWait(driver, 8).until(EC.presence_of_element_located((By.XPATH, xpath)))
    except Exception:
        return False

    checked = (checkbox.get_attribute("aria-checked") or "").lower() == "true"
    if checked == enabled:
        return True

    driver.execute_script(
        """
        const box = arguments[0];
        box.scrollIntoView({block: 'center'});
        """,
        checkbox,
    )
    time.sleep(0.5)
    driver.execute_script(
        """
        const box = arguments[0];
        const rect = box.getBoundingClientRect();
        const x = rect.right - 18;
        const y = rect.top + rect.height / 2;
        const target = document.elementFromPoint(x, y) || box;
        for (const type of ['pointerdown', 'mousedown', 'pointerup', 'mouseup', 'click']) {
            target.dispatchEvent(new MouseEvent(type, {
                bubbles: true,
                cancelable: true,
                view: window,
                clientX: x,
                clientY: y,
            }));
        }
        """,
        checkbox,
    )
    time.sleep(0.5)
    return ((checkbox.get_attribute("aria-checked") or "").lower() == "true") == enabled


def configure_meetup_preferences(driver: webdriver.Chrome, args: argparse.Namespace) -> None:
    preferences = [
        ("Public meetup", args.meet_public),
        ("Door pickup", args.door_pickup),
        ("Door dropoff", args.door_dropoff),
    ]
    for label, enabled in preferences:
        if enabled:
            ok = set_checkbox(driver, label, True)
            log(f"{label}: {'OK' if ok else 'NO ENCONTRADO'}")


def dropdown_script_body() -> str:
    return """
        const wanted = (arguments[0] || '').trim();
        const wantedLower = wanted.toLowerCase();
        const selectors = 'span[dir="auto"], div[role="option"], [role="menuitem"]';

        function textOf(el) {
            return (el.innerText || el.getAttribute('aria-label') || '').trim();
        }

        function visible(el) {
            const r = el.getBoundingClientRect();
            const style = getComputedStyle(el);
            return r.width > 0 && r.height > 0
                && r.x >= -5 && r.x < 380
                && r.y >= -5 && r.y < innerHeight
                && style.display !== 'none'
                && style.visibility !== 'hidden'
                && Number(style.opacity) > 0;
        }

        const containers = [...document.querySelectorAll('div')]
            .filter(el => {
                const r = el.getBoundingClientRect();
                const text = textOf(el);
                return text && visible(el)
                    && r.width >= 180 && r.width <= 380
                    && r.height >= 40
                    && r.x >= -5 && r.x < 380;
            })
            .sort((a, b) => {
                const ar = a.getBoundingClientRect();
                const br = b.getBoundingClientRect();
                return (ar.width * ar.height) - (br.width * br.height);
            });

        function scrollPositions(container) {
            const max = Math.max(0, container.scrollHeight - container.clientHeight);
            const positions = [0];
            for (let pos = 120; pos < max; pos += 120) positions.push(pos);
            positions.push(max);
            return [...new Set(positions)];
        }
    """


def visible_option_texts(driver: webdriver.Chrome) -> list[str]:
    texts = driver.execute_script(
        dropdown_script_body()
        + """
        const seen = [];
        for (const container of containers) {
            for (const pos of scrollPositions(container)) {
                container.scrollTop = pos;
                container.dispatchEvent(new Event('scroll', {bubbles: true}));
                for (const node of [...container.querySelectorAll(selectors)]) {
                    const text = textOf(node);
                    if (text && text.length < 80 && visible(node) && !seen.includes(text)) {
                        seen.push(text);
                    }
                }
            }
        }
        return seen.slice(0, 120);
        """,
        "",
    )
    seen = []
    for text in texts:
        if text not in seen:
            seen.append(text)
    return seen


def click_open_dropdown_option(driver: webdriver.Chrome, value: str) -> bool:
    clicked = driver.execute_script(
        dropdown_script_body()
        + """
        function isMenuContainer(el) {
            const rect = el.getBoundingClientRect();
            const role = el.getAttribute('role') || '';
            return rect.width >= 180 && rect.width <= 380
                && rect.height >= 40
                && rect.x >= -5 && rect.x < 420
                && rect.y >= -5 && rect.y < innerHeight
                && (role === 'listbox' || role === 'menu' || el.scrollHeight > el.clientHeight + 20);
        }

        const menuContainers = [...document.querySelectorAll('div')]
            .filter(el => isMenuContainer(el))
            .sort((a, b) => {
                const aScroll = a.scrollHeight - a.clientHeight;
                const bScroll = b.scrollHeight - b.clientHeight;
                return bScroll - aScroll;
            });

        function optionRow(node, container) {
            let best = node;
            let current = node;
            while (current && current.parentElement && current.parentElement !== document.body && current !== container) {
                const parent = current.parentElement;
                if (!visible(parent)) break;
                const text = textOf(parent);
                const rect = parent.getBoundingClientRect();
                const matchesText = text.toLowerCase() === wantedLower
                    || text.toLowerCase().startsWith(wantedLower + '\\n')
                    || text.toLowerCase().includes(wantedLower);
                if (matchesText && rect.width >= 120 && rect.height >= 18 && rect.height <= 90
                    && rect.x >= -5 && rect.x < 380) {
                    best = parent;
                }
                if (parent.scrollHeight > parent.clientHeight + 20) break;
                current = parent;
            }
            return best;
        }

        function findAndClick(exact) {
            const searchContainers = [...new Set([...menuContainers, ...containers])];
            for (const container of searchContainers) {
                for (const pos of scrollPositions(container)) {
                    container.scrollTop = pos;
                    container.dispatchEvent(new Event('scroll', {bubbles: true}));
                    for (const node of [...container.querySelectorAll(selectors)]) {
                        const text = textOf(node);
                        if (!text || !visible(node)) continue;
                        const normalized = text.toLowerCase();
                        const matches = exact
                            ? normalized === wantedLower
                            : normalized.includes(wantedLower);
                        if (matches) {
                            const row = optionRow(node, container);
                            const rect = row.getBoundingClientRect();
                            const x = rect.left + Math.min(rect.width / 2, 80);
                            const y = rect.top + rect.height / 2;
                            const target = document.elementFromPoint(x, y) || row;
                            for (const type of ['pointerdown', 'mousedown', 'pointerup', 'mouseup', 'click']) {
                                target.dispatchEvent(new MouseEvent(type, {
                                    bubbles: true,
                                    cancelable: true,
                                    view: window,
                                    clientX: x,
                                    clientY: y,
                                }));
                            }
                            return true;
                        }
                    }
                }
            }
            return false;
        }

        return findAndClick(true) || findAndClick(false);
        """,
        value,
    )
    if not clicked:
        return False
    time.sleep(0.8)
    return True


def choose_dropdown_option(driver: webdriver.Chrome, label: str, value: str) -> bool:
    close_floating_panels(driver)
    if not open_combobox(driver, label):
        return False
    return click_open_dropdown_option(driver, value)


def wait_for_manual_category(driver: webdriver.Chrome, timeout: int) -> None:
    log("Selecciona la categoria manualmente en Facebook. Esperando...")
    end = time.time() + timeout
    while time.time() < end:
        page_text = driver.find_element(By.TAG_NAME, "body").text
        if "Category" in page_text and "Next" in page_text:
            # Cannot reliably infer the selected category from the text, so wait
            # until Next becomes enabled.
            buttons = driver.find_elements(By.XPATH, "//div[@role='button' or self::button][.//*[normalize-space()='Next'] or normalize-space()='Next']")
            for button in buttons:
                disabled = button.get_attribute("aria-disabled") or button.get_attribute("disabled")
                if button.is_displayed() and not disabled:
                    log("Next parece habilitado; categoria completada.")
                    return
        time.sleep(2)
    raise TimeoutError("No detecte categoria completada antes del timeout.")


def main() -> None:
    parser = argparse.ArgumentParser(description="Llena Facebook Marketplace usando una sesion abierta del navegador.")
    parser.add_argument("--excel", type=Path, default=DEFAULT_EXCEL)
    parser.add_argument("--images-root", type=Path, default=DEFAULT_IMAGES)
    parser.add_argument("--row-index", type=int, default=0)
    parser.add_argument("--chrome-profile", type=Path, default=DEFAULT_PROFILE)
    parser.add_argument("--debugger-address", default="")
    parser.add_argument("--category", default="")
    parser.add_argument("--condition", default="")
    parser.add_argument("--manual-category", action="store_true")
    parser.add_argument("--manual-category-timeout", type=int, default=180)
    parser.add_argument("--inspect-categories", action="store_true")
    parser.add_argument("--meet-public", action="store_true", help="Activa Public meetup.")
    parser.add_argument("--door-pickup", action="store_true", help="Activa Door pickup.")
    parser.add_argument("--door-dropoff", action="store_true", help="Activa Door dropoff.")
    parser.add_argument("--skip-sku-field", action="store_true", help="No llena el campo SKU interno de Facebook.")
    parser.add_argument("--strict-details", action="store_true", help="Falla si no completa descripcion o etiquetas.")
    parser.add_argument("--publish", action="store_true", help="Presiona Next y avanza al paso final. No hace clic final en Publish.")
    parser.add_argument("--confirm-publish", action="store_true", help="Publica realmente el articulo. Usar solo con confirmacion explicita.")
    parser.add_argument("--start-maximized", action="store_true", default=True)
    args = parser.parse_args()

    driver = None
    stage = "read-product"
    try:
        product = read_product(args.excel, args.images_root, args.row_index)
        if args.category:
            product["category"] = args.category
        if args.condition:
            product["condition"] = args.condition

        stage = "open-session"
        driver = chrome_driver(args)
        driver.get(CREATE_URL)
        ensure_marketplace_session(driver)
        log(f"Formulario abierto para SKU={product['sku']} titulo={product['title']}")

        stage = "photos"
        upload_photo(driver, product["images"])
        stage = "required-fields"
        fill_required_text_fields(driver, product)

        stage = "condition"
        condition_ok = choose_dropdown_option(driver, "Condition", product["condition"])
        log(f"Condition {product['condition']}: {'OK' if condition_ok else 'NO ENCONTRADA'}")
        if not condition_ok:
            raise RuntimeError(f"No pude seleccionar Condition={product['condition']}.")

        stage = "category"
        if args.inspect_categories:
            choose_dropdown_option(driver, "Category", product["category"])
            print("\nOpciones visibles detectadas:")
            for text in visible_option_texts(driver):
                print(f"- {text}")
            return

        category_ok = choose_dropdown_option(driver, "Category", product["category"])
        log(f"Category {product['category']}: {'OK' if category_ok else 'NO ENCONTRADA'}")
        if not category_ok:
            print("\nOpciones visibles detectadas:")
            for text in visible_option_texts(driver):
                print(f"- {text}")
            if args.manual_category:
                wait_for_manual_category(driver, args.manual_category_timeout)
            else:
                raise RuntimeError(f"No pude seleccionar Category={product['category']}.")

        stage = "optional-fields"
        fill_optional_details(driver, product, fill_sku=not args.skip_sku_field, strict=args.strict_details)
        stage = "delivery-options"
        configure_meetup_preferences(driver, args)

        if not args.publish:
            log("Dry-run listo. No presione Next/Publish porque falta --publish.")
            return

        stage = "next"
        if not click_button(driver, "Next"):
            raise RuntimeError("No pude presionar Next.")
        log("Se presiono Next. Revisa cualquier paso final antes de publicar.")

        if not args.confirm_publish:
            return

        stage = "publish"
        if not click_button(driver, "Publish"):
            raise RuntimeError("No pude presionar Publish.")
        stage = "publish-confirmation"
        verify_publish_result(driver)
        log("Publicacion confirmada por Facebook Marketplace.")
    except Exception as exc:
        screenshot = capture_failure(driver, stage, exc)
        diagnostic = f"[error-code:{error_code(exc)}] stage={stage}"
        if screenshot:
            diagnostic += f" screenshot={screenshot}"
        print(f"[marketplace-browser] {diagnostic}", file=sys.stderr, flush=True)
        raise


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        raise
    except Exception as exc:  # noqa: BLE001 - command line tool should report clearly.
        print(f"[marketplace-browser] ERROR: {exc}", file=sys.stderr)
        raise SystemExit(1)
