"""Drawing the wallpaper: Pillow compositing, fonts and text overlays.

Pure image work with no knowledge of Immich, the desktop or the history:
decode a photo, letterbox or lay several out on a canvas, and draw the
caption and date overlays. Pillow is imported lazily, inside the functions
that need it, so the control commands in rotate.py work without it.
"""
from __future__ import annotations

import time
from datetime import datetime
from io import BytesIO
from pathlib import Path

import desktops
import layout

# Pixel gap between the two photos of a side-by-side portrait pair.
PAIR_GAP_PX = 6

# Largest photo (in pixels) we will decode; a 100 megapixel image is already
# larger than any consumer camera produces.
MAX_DECODE_PIXELS = 100_000_000

# Distance kept clear of every screen edge, on top of any taskbar/panel.
EDGE_MARGIN_INCHES = 0.5

# One-pixel offsets in all eight directions, used to fake a text outline.
_OUTLINE_OFFSETS = (
    (-1, 0), (1, 0), (0, -1), (0, 1), (-1, -1), (1, 1), (-1, 1), (1, -1),
)

_FONT_CANDIDATES = [
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    "/usr/share/fonts/TTF/DejaVuSans.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf",
    "/usr/share/fonts/liberation-fonts/LiberationSans-Regular.ttf",
    "/usr/share/fonts/truetype/noto/NotoSans-Regular.ttf",
]


def load_oriented(data: bytes):
    """Decode image `data` to an upright RGB PIL image.

    EXIF rotation is applied.
    """
    import warnings

    from PIL import Image, ImageOps
    # The bytes come from a remote server, so refuse a "decompression bomb":
    # a small file that expands to a huge image. Pillow only warns above its
    # limit, so make that warning an error.
    Image.MAX_IMAGE_PIXELS = MAX_DECODE_PIXELS
    with warnings.catch_warnings():
        warnings.simplefilter("error", Image.DecompressionBombWarning)
        image = Image.open(BytesIO(data))
        image = ImageOps.exif_transpose(image)
        return image.convert("RGB")


def contain_resize(image, target_width: int, target_height: int):
    """Scale `image` to fit entirely within the target size.

    No cropping -- the caller's canvas shows through as letterbox bars
    around it.
    """
    from PIL import Image
    scale = min(target_width / image.width, target_height / image.height)
    new_width = max(1, round(image.width * scale))
    new_height = max(1, round(image.height * scale))
    return image.resize((new_width, new_height), Image.LANCZOS)


def letterbox_single(
    image, target_width: int, target_height: int, background=(0, 0, 0),
):
    """Center `image` on a canvas of the target size (see contain_resize)."""
    from PIL import Image
    canvas = Image.new("RGB", (target_width, target_height), background)
    fitted = contain_resize(image, target_width, target_height)
    canvas.paste(fitted, ((target_width - fitted.width) // 2,
                          (target_height - fitted.height) // 2))
    return canvas


def compose_pair(
    data_left: bytes, data_right: bytes, target_width: int,
    target_height: int, gap: int = PAIR_GAP_PX, background=(0, 0, 0),
):
    """Place two photos side by side on one canvas of the target size.

    Each photo is letterboxed inside its own half.
    """
    from PIL import Image
    canvas = Image.new("RGB", (target_width, target_height), background)
    left_width = (target_width - gap) // 2
    right_width = target_width - gap - left_width

    left = contain_resize(
        load_oriented(data_left), left_width, target_height)
    canvas.paste(left, ((left_width - left.width) // 2,
                        (target_height - left.height) // 2))

    right = contain_resize(
        load_oriented(data_right), right_width, target_height)
    right_x = left_width + gap + (right_width - right.width) // 2
    canvas.paste(right, (right_x, (target_height - right.height) // 2))
    return canvas


def compose_row(
    photos: list[bytes], placements: list[layout.Placement],
    target_width: int, target_height: int, background=(0, 0, 0),
):
    """Draw photos at planned positions on one canvas of the target size.

    `placements` come from layout.plan_row(); each photo is fitted whole
    inside its rectangle, so nothing is cropped.
    """
    from PIL import Image
    canvas = Image.new("RGB", (target_width, target_height), background)
    for data, place in zip(photos, placements):
        image = contain_resize(
            load_oriented(data), place.width, place.height)
        canvas.paste(image, (place.x + (place.width - image.width) // 2,
                             place.y + (place.height - image.height) // 2))
    return canvas


def _load_font(size: int):
    """Load the first available known TrueType font at `size` pixels.

    Falls back to Pillow's built-in font if none of them can be opened.
    """
    from PIL import ImageFont
    for path in _FONT_CANDIDATES:
        if Path(path).exists():
            try:
                return ImageFont.truetype(path, size)
            except OSError:
                continue
    try:
        return ImageFont.load_default(size=size)
    except TypeError:
        return ImageFont.load_default()  # older Pillow: fixed small size


def _draw_outlined_text(
    draw, position: tuple[int, int], text: str, font,
    fill=(190, 190, 190), outline=(0, 0, 0),
) -> None:
    """Draw `text` at `position` in `fill` with a one-pixel `outline`."""
    x, y = position
    for offset_x, offset_y in _OUTLINE_OFFSETS:
        draw.text((x + offset_x, y + offset_y), text, font=font, fill=outline)
    draw.text((x, y), text, font=font, fill=fill)


def _format_taken_date(iso_timestamp: str | None) -> str | None:
    """Format an ISO-8601 timestamp as e.g. "Aug 29, 2022", or None."""
    if not iso_timestamp:
        return None
    try:
        taken = datetime.fromisoformat(iso_timestamp.replace("Z", "+00:00"))
        return taken.strftime("%b %-d, %Y")
    except (ValueError, TypeError):
        return None


def photo_caption_lines(details: dict | None) -> list[str]:
    """Build caption lines from a full asset record, top to bottom.

    The lines are people names, location and date taken. Missing parts are
    skipped; the list is empty if there is nothing to show.
    """
    if not details:
        return []
    exif = details.get("exifInfo") or {}
    lines = []
    visible_names = [
        person["name"] for person in (details.get("people") or [])
        if person.get("name") and not person.get("isHidden")
    ]
    if visible_names:
        lines.append(", ".join(visible_names))
    place_parts = (exif.get("city"), exif.get("state") or exif.get("country"))
    location = ", ".join(part for part in place_parts if part)
    if location:
        lines.append(location)
    date_taken = _format_taken_date(exif.get("dateTimeOriginal"))
    if date_taken:
        lines.append(date_taken)
    return lines


def edge_margin_px() -> int:
    """Return the base margin kept clear of screen edges, in pixels."""
    return round(desktops.get_screen_dpi() * EDGE_MARGIN_INCHES)


def draw_caption(
    canvas, lines: list[str], region_left: int, region_right: int,
    region_bottom: int, corner: str = "left", extra_x: int = 0,
    extra_bottom: int = 0,
) -> None:
    """Draw caption `lines` bottom-anchored inside a horizontal region.

    The region [region_left, region_right] is separate from the whole
    canvas so pair captions stay against their own half. Lines are
    right-aligned if `corner` is "right". `extra_x`/`extra_bottom` pad past
    a detected taskbar/panel on top of the base EDGE_MARGIN_INCHES -- see
    screen_insets().
    """
    if not lines:
        return
    from PIL import ImageDraw
    draw = ImageDraw.Draw(canvas)
    font_size = max(13, round(canvas.height * 0.022))
    font = _load_font(font_size)
    margin = edge_margin_px()
    line_gap = max(2, font_size // 6)
    measured = [(line, draw.textbbox((0, 0), line, font=font))
                for line in lines]
    total_height = (
        sum(box[3] - box[1] for _, box in measured)
        + line_gap * (len(lines) - 1)
    )
    y = region_bottom - (margin + extra_bottom) - total_height
    for line, box in measured:
        text_width = box[2] - box[0]
        if corner == "right":
            x = region_right - (margin + extra_x) - text_width
        else:
            x = region_left + margin + extra_x
        _draw_outlined_text(draw, (x, y), line, font)
        y += (box[3] - box[1]) + line_gap


def draw_date_overlay(canvas, extra_x: int = 0, extra_top: int = 0) -> None:
    """Draw today's date, top-left.

    Baked in at rotation time -- see the overlay note above on why this
    isn't a live clock. `extra_x`/`extra_top` pad past a detected
    taskbar/panel on top of the base EDGE_MARGIN_INCHES -- see
    screen_insets().
    """
    from PIL import ImageDraw
    draw = ImageDraw.Draw(canvas)
    font_size = max(15, round(canvas.height * 0.026))
    font = _load_font(font_size)
    margin = edge_margin_px()
    _draw_outlined_text(
        draw, (margin + extra_x, margin + extra_top),
        time.strftime("%A, %B %-d"), font)
