"""Varias publicaciones del mismo producto, cada una con fotos distintas.

Sin esto, un producto sube todas sus fotos seleccionadas en un solo anuncio y,
como una foto no se repite en la misma cuenta, el producto ya no vuelve a
programarse aunque tenga fotos de sobra. Con ``photos_per_listing`` el bot trata
las fotos del producto como un banco y reparte el banco en rondas: cada anuncio
toma las siguientes fotos sin usar de esa cuenta.

- El reparto es parejo: con 10 fotos y hasta 3 por anuncio salen 4 anuncios de
  3, 3, 2 y 2 fotos, y nunca uno final con una sola foto.
- La portada rota en cada ronda (123, 231, 312...) para que dos anuncios del
  mismo producto no abran con la misma foto.
- Con varias cuentas, cada una empieza en un bloque distinto del banco para que
  no suban lo mismo a la vez.

La regla de fondo no cambia: una foto (por contenido) sale una sola vez por cuenta.
"""
from __future__ import annotations

from typing import Callable, Iterable

MAX_LISTING_PHOTOS = 10   # lo que admite un anuncio de Marketplace
MAX_POOL_PHOTOS = 60      # fotos que se pueden reservar para un producto y cuenta


def next_round_size(remaining: int, per_listing: int) -> int:
    """Fotos de la siguiente ronda: reparto parejo con un tope por anuncio."""
    if remaining <= 0 or per_listing <= 0:
        return 0
    rounds = -(-remaining // per_listing)
    return -(-remaining // rounds)


def plan_round(
    pool: Iterable[str],
    *,
    per_listing: int,
    hash_of: Callable[[str], str],
    blocked: set[str],
    account_index: int = 0,
    round_number: int = 0,
    rotate_cover: bool = True,
    stagger: bool = True,
) -> list[str]:
    """Fotos de la siguiente publicacion de un producto en una cuenta.

    ``pool`` es el banco ordenado, ``blocked`` los hashes que esa cuenta ya uso o
    tiene reservados, y ``round_number`` cuantos anuncios de este producto lleva
    la cuenta. Devuelve una lista vacia cuando no quedan fotos nuevas.
    """
    paths = [str(path) for path in pool]
    if not paths or per_listing <= 0:
        return []
    per_listing = min(per_listing, MAX_LISTING_PHOTOS)
    if stagger and account_index and len(paths) > 1:
        # El desplazamiento usa el banco completo (no el que queda) para que no
        # cambie a medida que se usan fotos.
        offset = (account_index * next_round_size(len(paths), per_listing)) % len(paths)
        paths = paths[offset:] + paths[:offset]

    remaining: list[str] = []
    seen: set[str] = set()
    for path in paths:
        try:
            digest = hash_of(path)
        except OSError:
            continue
        if digest in blocked or digest in seen:
            continue
        seen.add(digest)
        remaining.append(path)

    chunk = remaining[: next_round_size(len(remaining), per_listing)]
    if rotate_cover and len(chunk) > 1:
        shift = round_number % len(chunk)
        chunk = chunk[shift:] + chunk[:shift]
    return chunk
