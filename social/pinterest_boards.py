"""Which Pinterest board a Pin is saved to: the board for the one product it shows, else the default board."""

import re

from .library import products
from .models import Asset
from .product_rules import normalize

BOARD_ID = re.compile(r"\d{1,40}")


def product_names():
    """Each Builder product's ID, name and aliases, normalized, mapped to its product ID."""
    from .real_sources import _product_aliases

    return _product_aliases()


def product_boards(settings):
    """PINTEREST_PRODUCT_BOARDS as {Builder product ID: board ID}."""
    names, boards = product_names(), {}
    for entry in filter(None, (e.strip() for e in settings.pinterest_product_boards.split(","))):
        name, _, board = (part.strip() for part in entry.partition("="))
        product = names.get(normalize(name))
        if not product:
            known = ", ".join(sorted(set(names.values())))
            raise ValueError(f"PINTEREST_PRODUCT_BOARDS: '{name[:60]}' isn't a product. Use one of {known}.")
        if not BOARD_ID.fullmatch(board):
            raise ValueError(f"PINTEREST_PRODUCT_BOARDS: the board for {product} must be its numeric ID")
        if boards.setdefault(product, board) != board:
            raise ValueError(f"PINTEREST_PRODUCT_BOARDS gives {product} two boards")
    return boards


def named_in(text, names):
    """Builder products named in a folder path or file name. Longer names are read first, so a
    "Drive-Through Canopy" folder is a drive-through, not also a canopy."""
    text, found = normalize(text), set()
    for name in sorted(names, key=len, reverse=True):
        pattern = rf"(?<!\w){re.escape(name)}(?!\w)"
        if re.search(pattern, text):
            found.add(names[name])
            text = re.sub(pattern, " ", text)
    return found


def originals(db, assets):
    """The original photos and videos a Pin's media was made from: not its copies, logo, music or sound."""
    from .asset_edit import ancestry

    sources, _ = ancestry(db, [a for a in assets if a])
    added = set()
    for a in sources:
        tags = a.tags or {}
        added |= {(tags.get(k) or {}).get("asset_id") for k in ("end_card", "ambient_music")}
        edit = tags.get("standard_edit") or {}
        shown = {i.get("asset_id") for i in edit.get("items", [])}
        added |= {c.get("asset_id") for c in edit.get("audio", [])} - shown
    return [
        a for a in sources
        if "rendition" not in (a.tags or {}) and a.id not in added and not (a.tags or {}).get("brand_logo")
        and a.media.get("mime", "").startswith(("image/", "video/"))
    ]


def shown_product(asset, names):
    """The one product an original shows: from its product tags, or when it has none, from its Google
    Drive folder path and file name. "" when that isn't one known product."""
    tags = products(asset)
    if tags:
        found = {names.get(normalize(t), "") for t in tags}
    else:
        drive = (asset.media or {}).get("drive_search") or {}
        found = named_in(f"{drive.get('folder_path', '')} {drive.get('name', '')}", names)
    return found.pop() if len(found) == 1 else ""


def pin_product(db, post):
    """The one product a Pin shows: the post's own product, else the product every original agrees on."""
    names = product_names()
    if product := names.get(normalize(post.product)):
        return product
    found = {shown_product(a, names) for a in originals(db, [db.get(Asset, i) for i in post.asset_ids])}
    return found.pop() if len(found) == 1 else ""


def board_for(db, settings, post):
    """The product's board when the Pin shows one product that has a board, else PINTEREST_BOARD_ID."""
    boards = product_boards(settings)
    return (boards and boards.get(pin_product(db, post))) or settings.pinterest_board_id
