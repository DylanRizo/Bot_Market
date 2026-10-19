"""Redaccion variada de anuncios: titulo, descripcion y etiquetas.

Facebook marca como duplicados los anuncios con el mismo texto aunque cambien
las fotos. Cada publicacion arma su texto con piezas elegidas por una semilla
(cuenta, familia, dia) y, entre varias versiones, se queda con la que menos se
parece a lo publicado en las ultimas semanas por cualquier cuenta y a los
titulos que siguen en Facebook.

Solo cambia la forma: nombre del producto, colores, tallas, precio y ubicacion
salen de los datos del SGI. No inventa materiales, medidas ni beneficios.
Sin semilla el texto es siempre el mismo (anuncios manuales y vistas previas).
"""
from __future__ import annotations

import hashlib
import random
import re
import unicodedata
from dataclasses import dataclass
from difflib import SequenceMatcher
from typing import Any, Iterable

TITLE_MAX = 99
TITLE_TARGET = 70
CANDIDATES = 24
SIZE_ORDER = ["XS", "S", "M", "L", "XL", "XXL"]
COLOR_DISPLAY = {"cafe": "café", "varios colores": "varios colores"}


@dataclass(frozen=True)
class Profile:
    root: str  # nombre del SGI sin tildes, en minusculas: "camisa de compresion manga corta"
    names: tuple[str, ...]  # formas de llamar al producto; {extra} = lo que sigue a root
    gender: str  # "m" o "f"
    plural: bool
    hooks: tuple[str, ...]  # {N} nombre con mayuscula, {n} en minuscula, {nuevo} concordado
    uses: tuple[str, ...]
    tags: tuple[str, ...]
    qualifiers: tuple[str, ...] = ()


def _p(root, names, gender, plural, hooks, uses, tags, qualifiers=()) -> Profile:
    return Profile(root, tuple(names), gender, plural, tuple(hooks), tuple(uses), tuple(tags), tuple(qualifiers))


GENERIC_HOOKS = (
    "{N} {nuevo}, listo para estrenar.",
    "Tenemos {un}{n} {nuevo} disponible.",
    "{N} en excelente estado: {nuevo}, sin usar.",
)

PROFILES: dict[str, Profile] = {
    "compression_short": _p(
        "camisa de compresion manga corta",
        ["Camisa de compresión manga corta", "Camiseta de compresión manga corta", "Camisa deportiva de compresión",
         "Camisa de compresión para gym"],
        "f", False,
        ["{N} {nueva}, ideal para entrenar o usar debajo de otra prenda.",
         "¿Buscas una camisa de compresión para el gym? Tenemos {un}{n} {nueva}.",
         "{N} {nueva}: se ajusta al cuerpo y te acompaña en cada rutina.",
         "Si entrenas seguido, esta {n} es para ti. Producto nuevo."],
        ["Sirve para gym, correr o cualquier deporte.", "Se puede usar sola o como primera capa.",
         "Cómoda para entrenar con libertad de movimiento.", "Combina con shorts, joggers o leggins."],
        ["camisa de compresión", "camiseta de compresión", "ropa de gym", "ropa deportiva", "gimnasio",
         "entrenamiento", "manga corta", "fitness", "compresión"],
        ["para gym", "deportiva", "para entrenar"],
    ),
    "compression_sleeveless": _p(
        "camisa sin mangas",
        ["Camisa sin mangas", "Camiseta sin mangas", "Camisa sin mangas para gym", "Camisa deportiva sin mangas"],
        "f", False,
        ["{N} {nueva}, cómoda para entrenar con libertad en los brazos.",
         "¿Te gusta entrenar fresco? Esta {n} {nueva} es para ti.",
         "{N} {nueva}, perfecta para el gym o para correr."],
        ["Ideal para rutinas de brazo, hombro o cardio.", "Combina con shorts o joggers.",
         "Se puede usar sola o debajo de otra prenda."],
        ["camisa sin mangas", "camiseta sin mangas", "ropa de gym", "ropa deportiva", "gimnasio",
         "entrenamiento", "fitness"],
        ["para gym", "deportiva"],
    ),
    "compression_long": _p(
        "camisa de compresion manga larga",
        ["Camisa de compresión manga larga", "Camiseta de compresión manga larga", "Camisa deportiva manga larga"],
        "f", False,
        ["{N} {nueva}, ideal para entrenar o usar como primera capa.",
         "¿Buscas manga larga para entrenar? Tenemos {un}{n} {nueva}.",
         "{N} {nueva}, se ajusta al cuerpo y cubre los brazos."],
        ["Sirve para gym, correr o deportes al aire libre.", "Se puede usar sola o debajo de otra prenda."],
        ["camisa de compresión", "manga larga", "ropa deportiva", "ropa de gym", "gimnasio", "entrenamiento",
         "primera capa"],
        ["para gym", "deportiva"],
    ),
    "bershka": _p(
        "camisa bersek",
        ["Camisa Bersek", "Camisa marca Bersek"],
        "f", False,
        ["{N} {nueva}, con estilo para el gym o para salir.",
         "Llegaron camisas Bersek {nuevas}, listas para estrenar.",
         "{N} {nueva} para completar tu outfit."],
        ["Combina con jeans, joggers o shorts.", "Para el día a día o para entrenar."],
        ["camisa Bersek", "Bersek", "ropa de hombre", "estilo urbano", "camisa", "outfit"],
    ),
    "chalecos": _p(
        "chaleco de lana sin mangas",
        ["Chaleco de lana sin mangas", "Chaleco de lana", "Chaleco sin mangas de lana", "Chaleco de lana formal"],
        "m", False,
        ["{N} {nuevo}, para combinar con camisa o con un look casual.",
         "¿Buscas un chaleco elegante? Tenemos {un}{n} {nuevo}.",
         "{N} {nuevo}, ideal para la oficina, la U o una salida."],
        ["Se ve bien sobre camisa manga larga o camiseta.", "Para un look formal o casual."],
        ["chaleco de lana", "chaleco", "chaleco sin mangas", "ropa formal", "outfit", "moda"],
        ["formal", "elegante"],
    ),
    "leggins": _p(
        "leggins deportivos",
        ["Leggins deportivos", "Leggins para gym", "Leggins de entrenamiento", "Leggings deportivos"],
        "m", True,
        ["{N} {nuevos}, con estilo deportivo y corte favorecedor.",
         "¿Buscas leggins para entrenar? Tenemos {un}{n} {nuevos}.",
         "{N} {nuevos}, cómodos para el gym o para un look casual."],
        ["Para gym, yoga, correr o el día a día.", "Combinan con tops o camisetas deportivas."],
        ["leggins", "leggins deportivos", "ropa deportiva mujer", "ropa de gym", "gimnasio", "yoga",
         "entrenamiento"],
    ),
    "enterizos": _p(
        "enterizo deportivo",
        ["Enterizo deportivo", "Enterizo corto de gym", "Enterizo para entrenar", "Enterizo deportivo corto"],
        "m", False,
        ["{N} {nuevo}, ideal para entrenar o crear un outfit casual.",
         "¿Te gustan los enterizos? Tenemos {un}{n} {nuevo}.",
         "{N} {nuevo}: una sola pieza, cómoda y con estilo."],
        ["Para gym, yoga o salir.", "Fácil de combinar con tenis o una chaqueta."],
        ["enterizo deportivo", "enterizo", "ropa deportiva mujer", "outfit deportivo", "gimnasio",
         "entrenamiento", "yoga"],
    ),
    "enterizos_largos": _p(
        "enterizo largo deportivo",
        ["Enterizo largo deportivo", "Enterizo largo de gym", "Enterizo largo para entrenar"],
        "m", False,
        ["{N} {nuevo}, cómodo y con estilo para entrenar.", "Tenemos {un}{n} {nuevo}, ideal para gym o yoga."],
        ["Una sola pieza, fácil de combinar.", "Para entrenar o para un look casual."],
        ["enterizo largo", "enterizo deportivo", "ropa deportiva mujer", "gimnasio", "yoga", "outfit deportivo"],
    ),
    "shorts": _p(
        "short deportivo",
        ["Short deportivo", "Short de gym", "Short para entrenar", "Short deportivo liso"],
        "m", False,
        ["{N} {nuevo}, cómodo y versátil para entrenar.", "¿Buscas un short para el gym? Tenemos {un}{n} {nuevo}.",
         "{N} {nuevo}, sirve para entrenar o para un look casual."],
        ["Para gym, correr o el día a día.", "Combina con camisas de compresión o sin mangas."],
        ["short deportivo", "short de gym", "ropa deportiva", "gimnasio", "entrenamiento", "shorts"],
    ),
    "shorts_youngla": _p(
        "short deportivo youngla",
        ["Short deportivo YoungLA", "Short YoungLA", "Short YoungLA para gym"],
        "m", False,
        ["{N} {nuevo}, cómodo para entrenar y con estilo.", "Llegaron shorts YoungLA {nuevos}."],
        ["Para gym o el día a día.", "Combina con camisas de compresión."],
        ["YoungLA", "short YoungLA", "short deportivo", "ropa de gym", "gimnasio", "entrenamiento"],
    ),
    "faldas": _p(
        "falda deportiva",
        ["Falda deportiva", "Falda deportiva para gym", "Falda para entrenar"],
        "f", False,
        ["{N} {nueva}, cómoda para entrenar o armar un look deportivo.", "Tenemos {un}{n} {nueva}, con estilo."],
        ["Para tenis, gym o salir.", "Combina con tops deportivos."],
        ["falda deportiva", "ropa deportiva mujer", "outfit deportivo", "falda", "gimnasio"],
    ),
    "tops": _p(
        "top deportivo",
        ["Top deportivo", "Top de gym", "Top para entrenar"],
        "m", False,
        ["{N} {nuevo}, para entrenar y combinar con leggins o shorts.",
         "¿Buscas un top para el gym? Tenemos {un}{n} {nuevo}."],
        ["Para gym, yoga o correr.", "Combina con leggins o enterizos."],
        ["top deportivo", "top de gym", "ropa deportiva mujer", "gimnasio", "yoga", "entrenamiento"],
    ),
    "bolsos": _p(
        "bolso deportivo",
        ["Bolso deportivo", "Bolso para gym", "Bolso de gimnasio", "Maletín deportivo"],
        "m", False,
        ["{N} {nuevo}, práctico para llevar ropa, tenis y accesorios.",
         "¿Necesitas un bolso para el gym? Tenemos {un}{n} {nuevo}.",
         "{N} {nuevo}, para el gimnasio, viajes cortos o el día a día."],
        ["Lleva tu ropa, calzado y botella en un solo lugar.", "Sirve para gym, deporte o viaje."],
        ["bolso deportivo", "bolso de gym", "maletín deportivo", "bolso de viaje", "accesorios deportivos",
         "gimnasio"],
    ),
    "mochilas": _p(
        "mochila antirrobo",
        ["Mochila antirrobo", "Mochila antirrobo para laptop"],
        "f", False,
        ["{N} {nueva}, práctica para el trabajo, la U o viajes.", "Tenemos {un}{n} {nueva} disponible."],
        ["Para el día a día, la oficina o la universidad."],
        ["mochila antirrobo", "mochila", "mochila para laptop", "accesorios", "viaje"],
    ),
    "durags": _p(
        "durag",
        ["Durag", "Durag para el cabello", "Durag de moda"],
        "m", False,
        ["{N} {nuevo}, para completar tu estilo y cuidar el cabello.", "¿Buscas un durag? Tenemos {un}{n} {nuevo}.",
         "{N} {nuevo}, ideal para uso diario o para dormir."],
        ["Ayuda a mantener el peinado en su lugar.", "Para el día a día o para un look urbano."],
        ["durag", "accesorios de cabello", "estilo urbano", "waves", "gorro", "moda urbana"],
    ),
    "munequeras": _p(
        "munequeras deportivas",
        ["Muñequeras deportivas", "Muñequeras para gym", "Muñequeras de entrenamiento"],
        "f", True,
        ["{N} {nuevas}, para entrenar con más comodidad y soporte.",
         "¿Haces rutinas de fuerza? Tenemos {un}{n} {nuevas}.",
         "{N} {nuevas}, ideales para press, pesas o crossfit."],
        ["Para rutinas de fuerza, gym o crossfit.", "Fáciles de ajustar antes de cada serie."],
        ["muñequeras", "muñequeras deportivas", "accesorios de gym", "gimnasio", "pesas", "crossfit",
         "entrenamiento"],
    ),
    "straps": _p(
        "straps para gimnasio",
        ["Straps para gimnasio", "Straps para levantar pesas", "Correas de levantamiento"],
        "m", True,
        ["{N} {nuevos}, para mejorar el agarre en espalda y peso muerto.",
         "¿Se te cansa el agarre? Tenemos {un}{n} {nuevos}."],
        ["Para peso muerto, remos y jalones.", "Un accesorio clave para rutinas de espalda."],
        ["straps", "straps para gym", "correas de levantamiento", "accesorios de gym", "pesas", "gimnasio"],
    ),
    "cinturones": _p(
        "cinturon de gimnasio",
        ["Cinturón de gimnasio", "Cinturón para levantar pesas", "Cinturón de pesas"],
        "m", False,
        ["{N} {nuevo}, pensado para rutinas de fuerza.",
         "¿Entrenas sentadilla o peso muerto? Tenemos {un}{n} {nuevo}.",
         "{N} {nuevo}, para tus rutinas de fuerza."],
        ["Para sentadilla, peso muerto y levantamientos.", "Un accesorio básico para entrenar fuerza."],
        ["cinturón de gimnasio", "cinturón de pesas", "accesorios de gym", "levantamiento de pesas",
         "gimnasio", "powerlifting"],
    ),
    "mangas_brazo": _p(
        "manga para brazo",
        ["Manga para brazo", "Manga deportiva para brazo", "Manga de brazo para deporte"],
        "f", False,
        ["{N} {nueva}, para básquet, gym o cualquier deporte.", "¿Juegas básquet? Tenemos {un}{n} {nueva}.",
         "{N} {nueva}, cómoda y con estilo deportivo."],
        ["Para básquet, fútbol, gym o correr.", "Se ajusta al brazo sin estorbar."],
        ["manga para brazo", "manga deportiva", "básquet", "accesorios deportivos", "deporte", "gimnasio"],
    ),
    "calcetas": _p(
        "calcetas de compresion",
        ["Calcetas de compresión{extra}", "Medias de compresión{extra}", "Calcetas de compresión altas{extra}"],
        "f", True,
        ["{N} {nuevas}, ideales para largas jornadas de pie o sentado.",
         "¿Pasas muchas horas de pie? Tenemos {un}{n} {nuevas}.",
         "{N} {nuevas}, para descanso y confort en el día a día."],
        ["Para el trabajo, viajes largos o después de entrenar.", "Ideales si trabajas muchas horas de pie."],
        ["calcetas de compresión", "medias de compresión", "medias de descanso", "compresión",
         "calcetas altas", "salud y bienestar"],
    ),
    "calcetas_multicolor": _p(
        "calcetas",
        ["Calcetas multicolor", "Calcetas con diseños divertidos", "Calcetas locas y divertidas"],
        "f", True,
        ["{N} {nuevas}, para darle color a tu outfit.", "¿Te gustan las calcetas con diseño? Tenemos {un}{n} {nuevas}.",
         "{N} {nuevas}, un detalle divertido para el día a día."],
        ["Un buen regalo para alguien especial.", "Diseños surtidos."],
        ["calcetas", "calcetas divertidas", "calcetas con diseño", "medias", "regalo", "moda"],
    ),
    "joggers": _p(
        "pantalon jogger",
        ["Pantalón jogger", "Jogger deportivo", "Pantalón jogger para gym"],
        "m", False,
        ["{N} {nuevo}, cómodo para entrenar o salir.", "Tenemos {un}{n} {nuevo}, con estilo urbano."],
        ["Para gym, correr o el día a día."],
        ["pantalón jogger", "jogger", "ropa deportiva", "estilo urbano", "gimnasio"],
    ),
    "rodilleras": _p(
        "rodilleras deportivas",
        ["Rodilleras deportivas", "Rodilleras para gym", "Rodilleras de entrenamiento"],
        "f", True,
        ["{N} {nuevas}, para entrenar o hacer deporte con más comodidad.", "Tenemos {un}{n} {nuevas} disponibles."],
        ["Para sentadillas, crossfit o deportes.", "Fáciles de poner y quitar."],
        ["rodilleras", "rodilleras deportivas", "accesorios de gym", "gimnasio", "crossfit", "deporte"],
    ),
}

ADJ = {("m", False): "nuevo", ("f", False): "nueva", ("m", True): "nuevos", ("f", True): "nuevas"}
ARTICLE = {("m", False): "un ", ("f", False): "una ", ("m", True): "", ("f", True): ""}
PRONOUN = {("m", False): "Lo", ("f", False): "La", ("m", True): "Los", ("f", True): "Las"}

NEW_LINES = ("Producto nuevo.", "Artículo nuevo, sin usar.", "Totalmente nuevo.", "Nuevo, listo para estrenar.")


@dataclass(frozen=True)
class Copy:
    title: str
    description: str
    tags: list[str]


class Picker:
    """Elecciones estables para una semilla; sin semilla, siempre la primera opcion."""

    def __init__(self, seed: str) -> None:
        self.rng = random.Random(int.from_bytes(hashlib.sha256(seed.encode("utf-8")).digest()[:8], "big")) if seed else None

    def one(self, options: Iterable[Any]) -> Any:
        options = list(options)
        return self.rng.choice(options) if self.rng else options[0]

    def chance(self, probability: float) -> bool:
        return bool(self.rng) and self.rng.random() < probability

    def shuffled(self, items: Iterable[Any]) -> list[Any]:
        items = list(items)
        if self.rng:
            self.rng.shuffle(items)
        return items

    def between(self, low: int, high: int) -> int:
        return self.rng.randint(low, high) if self.rng else low


def plain(text: str) -> str:
    text = unicodedata.normalize("NFD", str(text or ""))
    text = "".join(char for char in text if unicodedata.category(char) != "Mn")
    return re.sub(r"\s+", " ", text).strip().lower()


def join_human(values: list[str]) -> str:
    values = [value for value in values if value]
    if len(values) <= 1:
        return values[0] if values else ""
    return ", ".join(values[:-1]) + f" y {values[-1]}"


def sort_sizes(values: Iterable[str]) -> list[str]:
    unique = list(dict.fromkeys(str(value).strip().upper() for value in values if str(value or "").strip()))
    return sorted(unique, key=lambda size: (SIZE_ORDER.index(size) if size in SIZE_ORDER else len(SIZE_ORDER), size))


def color_name(color: str) -> str:
    """Color con mayuscula y tilde: "cafe" -> "Café"."""
    value = COLOR_DISPLAY.get(plain(color), str(color).strip())
    return value[:1].upper() + value[1:]


def detect_family(base: str) -> str:
    try:
        from marketplace_listings import family_for_title
    except Exception:  # noqa: BLE001 - sin el modulo de anuncios se usa solo el perfil generico
        return ""
    return family_for_title(base)


def product_names(base: str, profile: Profile | None) -> list[str]:
    """Formas de nombrar el producto. Si el nombre del SGI no es el esperado, se usa tal cual."""
    if not profile:
        return [base]
    normalized = plain(base)
    if not normalized.startswith(profile.root) or len(normalized) != len(base):
        return [base] if normalized != profile.root else list(_render_names(profile, ""))
    return list(_render_names(profile, base[len(profile.root):].rstrip()))


def _render_names(profile: Profile, extra: str) -> Iterable[str]:
    adjective = ADJ[(profile.gender, profile.plural)]
    for name in profile.names:
        yield name.format(extra=extra, nueva=adjective, nuevo=adjective).strip()


def _lower_first(text: str) -> str:
    # "Camisa Bersek" -> "camisa Bersek"; respeta marcas en mayuscula ("YoungLA").
    return text[:1].lower() + text[1:] if text[:1].isupper() and not text[1:2].isupper() else text


def build_title(names: list[str], colors: list[str], sizes: list[str], profile: Profile | None, pick: Picker) -> str:
    name = pick.one(names)
    if profile and profile.qualifiers and pick.chance(0.3) and not any(q in plain(name) for q in map(plain, profile.qualifiers)):
        name = f"{name} {pick.one(profile.qualifiers)}"
    shown = pick.shuffled(colors)
    c = join_human(shown)
    s = ", ".join(sizes)
    r = f"{sizes[0]} a {sizes[-1]}" if len(sizes) > 1 else (sizes[0] if sizes else "")
    tallas = "Tallas" if len(sizes) > 1 else "Talla"
    k = len(colors)
    formats: list[str]
    if colors and sizes:
        formats = [
            "{n} - {c} ({s})",
            "{n} | {tallas} {r} | {c}",
            "{n} en {c} - {tallas} {s}",
            "{n} {c} | {tallas} {r}",
            "{n}: {c}, {tallas_l} {s}",
            "{n} {tallas_l} {r} en {c}",
            "{n} ({c}) - {tallas} {r}",
        ]
        if k >= 3:
            formats += ["{n} en {k} colores - {tallas} {r}", "{n} | {k} colores | {tallas} {s}"]
    elif colors:
        formats = ["{n} - {c}", "{n} en {c}", "{n} | {c}", "{n} ({c})", "{n}: {c}"]
        if k >= 3:
            formats += ["{n} en {k} colores", "{n} - {k} colores disponibles"]
        else:
            formats += ["{n} color {c}" if k == 1 else "{n} colores {c}"]
    elif sizes:
        formats = ["{n} - {tallas} {s}", "{n} | {tallas} {r}", "{n} {tallas_l} {s}"]
    else:
        formats = ["{n}"]
    values = {"c": c, "s": s, "r": r, "k": k, "tallas": tallas, "tallas_l": tallas.lower()}
    title = pick.one(formats).format(n=name, **values)
    if len(title) > TITLE_TARGET:
        # Muy largo: se elige entre todas las combinaciones de nombre y forma que
        # caben, no siempre la mas corta (si no, todas las cuentas repetirian una).
        combos = [fmt.format(n=option, **values) for option in names for fmt in formats]
        fitting = [option for option in combos if len(option) <= TITLE_TARGET]
        allowed = fitting or [option for option in combos if len(option) <= TITLE_MAX]
        title = pick.one(allowed) if allowed else name[:TITLE_MAX]
    return title[:1].upper() + title[1:]


def _repeats(sentence: str, name: str) -> bool:
    """"Enterizo para entrenar nuevo, ideal para entrenar": la frase repite una palabra del nombre."""
    words = {word for word in plain(name).split() if len(word) >= 5}
    rest = plain(sentence).replace(plain(name), " ", 1)
    return any(re.search(rf"\b{re.escape(word)}\b", rest) for word in words)


def _what(colors: list[str], sizes: list[str]) -> tuple[str, str]:
    """("color y talla", "el color y la talla") segun las variantes del anuncio."""
    if len(colors) > 1 and sizes:
        return "color y talla", "el color y la talla"
    if sizes and len(sizes) > 1:
        return "talla", "la talla"
    if len(colors) > 1:
        return "color", "el color"
    return "", ""


def build_description(
    names: list[str],
    colors: list[str],
    sizes: list[str],
    prices: list[float],
    location: str,
    profile: Profile | None,
    pick: Picker,
    price_text,
) -> str:
    gender, plural = (profile.gender, profile.plural) if profile else ("m", False)
    adjective = ADJ[(gender, plural)]
    hooks = profile.hooks if profile else GENERIC_HOOKS
    template = pick.one(hooks)
    hook = ""
    for name in pick.shuffled(names):
        hook = template.format(
            N=name, n=_lower_first(name), un=ARTICLE[(gender, plural)],
            nuevo=adjective, nueva=adjective, nuevos=ADJ[(gender, True)], nuevas=ADJ[(gender, True)],
        )
        if not _repeats(hook, name):
            break
    lines_middle: list[str] = []
    if profile and profile.uses:
        # Sin repetir lo que ya dijo el gancho ("muchas horas de pie" dos veces).
        uses = [use for use in pick.shuffled(profile.uses) if not _shingle_similarity(hook, use)]
        lines_middle.extend(uses[: pick.between(1, min(2, len(uses)))] if uses else [])
    if "nuev" not in plain(hook) and pick.chance(0.5):
        lines_middle.append(pick.one(NEW_LINES))

    lower_colors = [color_name(color).lower() for color in pick.shuffled(colors)]
    c = join_human(lower_colors)
    s = join_human(sizes)
    pronoun = PRONOUN[(gender, plural)]
    if colors and sizes:
        variant_formats = [
            "Colores disponibles: {c}.\nTallas disponibles: {s}.",
            "Disponible en {c}, en tallas {s}.",
            "Colores: {c}\nTallas: {s}",
            "{pron} tenemos en {c}. Tallas: {s}.",
            "Colores:\n{bullets}\nTallas: {s}",
            "Tallas {s}, en {c}.",
        ]
    elif colors:
        variant_formats = (
            ["Color: {c}.", "Disponible en color {c}.", "{pron} tenemos en {c}."]
            if len(colors) == 1
            else ["Colores disponibles: {c}.", "Disponible en {c}.", "Colores: {c}", "{pron} tenemos en {c}.",
                  "Colores:\n{bullets}"]
        )
    elif sizes:
        variant_formats = ["Tallas disponibles: {s}.", "Tallas: {s}", "Disponible en tallas {s}."]
    else:
        variant_formats = [""]
    bullets = "\n".join(f"- {color[:1].upper()}{color[1:]}" for color in lower_colors)
    variants = pick.one(variant_formats).format(c=c, s=s, pron=pronoun, bullets=bullets)

    unique_prices = sorted({price for price in prices if price})
    if len(unique_prices) > 1:
        price_line = pick.one(["Precio desde {p}, según la variante.", "Desde {p} según color y talla.",
                               "Precios desde {p}."]).format(p=price_text(unique_prices[0]))
    else:
        amount = price_text(unique_prices[0] if unique_prices else 0)
        # Sin "por unidad": algunos productos se venden por par.
        price_options = ["Precio: {p}.", "Valor: {p}.", ("Cuestan {p}." if plural else "Cuesta {p}.")]
        if "tenemos" not in variants:
            price_options.append("{pron} tenemos a {p}.")
        price_line = pick.one(price_options).format(p=amount, pron=pronoun)
    place = location or "Managua, Nicaragua"
    city = place.split(",")[0].strip()
    location_line = pick.one(["Ubicación: {loc}.", "Estamos en {loc}.", "Entregas en {city}; coordinamos el punto por mensaje.",
                              "Punto de entrega a coordinar en {city}.", "Nos ubicamos en {loc}."]).format(loc=place, city=city)
    what, what_article = _what(colors, sizes)
    ctas = [
        "¿Te interesa? Escríbeme y coordinamos la entrega.",
        "Si te interesa, escríbeme y te confirmo disponibilidad.",
        "Escríbeme por mensaje y coordinamos la entrega.",
    ]
    if what_article:
        # Solo cuando hay que elegir color o talla.
        ctas = [
            "Escríbeme para confirmar {what} y coordinar la entrega.",
            "Mándame mensaje con {wa} que buscas y te confirmo disponibilidad.",
            "Consulta disponibilidad de {what} por mensaje; coordinamos la entrega.",
            "Envíame un mensaje con {wa} que necesitas.",
            "Pregunta por {wa} que buscas y coordinamos la entrega.",
            *ctas,
        ]
    cta = pick.one(ctas).format(what=what, wa=what_article)

    tail = pick.shuffled([price_line, location_line])
    sections = [[hook], pick.shuffled(lines_middle), [variants], tail, [cta]]
    separator = "\n\n" if pick.chance(0.5) else "\n"
    blocks = ["\n".join(line for line in section if line) for section in sections]
    blocks = [block for block in blocks if block]
    if separator == "\n\n":
        # Parrafos: gancho, cuerpo, cierre.
        return "\n\n".join([blocks[0], "\n".join(blocks[1:-1]), blocks[-1]]).replace("\n\n\n", "\n\n")
    return "\n".join(blocks)


def build_tags(names: list[str], colors: list[str], profile: Profile | None, pick: Picker) -> list[str]:
    pool = list(profile.tags) if profile else []
    pool += [_lower_first(re.sub(r"\s+\d.*$", "", name)) for name in names]
    pool = list(dict.fromkeys(pool))
    chosen = pick.shuffled(pool)[: pick.between(5, 6)] if pick.rng else pool[:6]
    for color in pick.shuffled(colors)[:2]:
        chosen.append(color_name(color).lower())
    return list(dict.fromkeys(chosen))[:8]


def similarity(copy: Copy, other: dict[str, str]) -> float:
    title = SequenceMatcher(None, plain(copy.title), plain(other.get("title", ""))).ratio()
    other_description = other.get("description") or ""
    if not other_description:
        return title
    return 0.5 * title + 0.5 * _shingle_similarity(copy.description, other_description)


def _shingle_similarity(first: str, second: str) -> float:
    def shingles(text: str) -> set[tuple[str, ...]]:
        words = re.findall(r"[a-z0-9$]+", plain(text))
        return {tuple(words[index:index + 3]) for index in range(max(len(words) - 2, 1))}

    a, b = shingles(first), shingles(second)
    return len(a & b) / len(a | b) if a and b else 0.0


def build_copy(
    base: str,
    colors: Iterable[str],
    sizes: Iterable[str],
    prices: Iterable[float],
    location: str,
    *,
    price_text,
    family: str = "",
    seed: str = "",
    recent: list[dict[str, str]] | None = None,
) -> Copy:
    colors = list(dict.fromkeys(color_name(color) for color in colors if str(color or "").strip()))
    sizes = sort_sizes(sizes)
    prices = [float(price) for price in prices if price]
    profile = PROFILES.get(family or detect_family(base))
    names = product_names(base, profile)

    def candidate(key: str) -> Copy:
        pick = Picker(key)
        return Copy(
            build_title(names, colors, sizes, profile, Picker(f"{key}|titulo") if key else pick),
            build_description(names, colors, sizes, prices, location, profile, Picker(f"{key}|texto") if key else pick, price_text),
            build_tags(names, colors, profile, Picker(f"{key}|etiquetas") if key else pick),
        )

    if not seed:
        return candidate("")
    options = [candidate(f"{seed}#{index}") for index in range(CANDIDATES)]
    if not recent:
        return options[0]

    def score(option: Copy) -> tuple[float, float]:
        values = [similarity(option, other) for other in recent]
        return (max(values), sum(values) / len(values))

    return min(options, key=score)
