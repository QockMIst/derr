import io
import textwrap
import urllib.request
from pathlib import Path
from typing import Any, Dict, List, Optional
from PIL import Image, ImageDraw, ImageFont

_THUMB_CACHE: Dict[str, Image.Image] = {}
FONTS_DIR = Path(__file__).parent / "web" / "fonts"


def _load_font(size: int = 14, mono: bool = False) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    font_file = FONTS_DIR / ("JetBrainsMono-Regular.ttf" if mono else "Inter-Regular.ttf")
    try:
        if font_file.exists():
            return ImageFont.truetype(str(font_file), size=size)
    except Exception:
        pass
    try:
        return ImageFont.truetype("segoeui.ttf", size=size)
    except Exception:
        return ImageFont.load_default()


def _fetch_thumb(url: str, size: tuple[int, int] = (72, 72)) -> Optional[Image.Image]:
    if not url:
        return None
    if url in _THUMB_CACHE:
        return _THUMB_CACHE[url].copy()
    try:
        req = urllib.request.Request(
            url,
            headers={"User-Agent": "Mozilla/5.0"},
        )
        with urllib.request.urlopen(req, timeout=6) as resp:
            img = Image.open(io.BytesIO(resp.read())).convert("RGBA")
            img = img.resize(size, Image.Resampling.LANCZOS)
            _THUMB_CACHE[url] = img
            return img.copy()
    except Exception:
        return None


def render_trade_card_png(
    offer_items: List[Dict[str, Any]],
    request_items: List[Dict[str, Any]],
    tags: Optional[List[str]] = None,
) -> bytes:
    """
    Renders a dark-themed Void-style Offer / Request comparison card as PNG bytes
    using bundled Inter and JetBrains Mono TrueType fonts.
    """
    width, height = 760, 280
    bg_color = (24, 26, 31)
    header_color = (34, 37, 44)
    slot_color = (43, 47, 56)
    text_white = (242, 243, 245)
    text_muted = (155, 162, 175)
    blue_val = (72, 168, 255)
    green_rap = (67, 198, 128)
    red_op = (240, 71, 71)

    img = Image.new("RGB", (width, height), bg_color)
    draw = ImageDraw.Draw(img)

    font_header = _load_font(15, mono=False)
    font_small = _load_font(11, mono=False)
    font_mono = _load_font(14, mono=True)
    font_footer = _load_font(13, mono=True)

    # Header bar
    draw.rectangle([0, 0, width, 38], fill=header_color)
    draw.text((160, 10), "Offer", fill=text_white, font=font_header)
    draw.text((530, 10), "Request", fill=text_white, font=font_header)

    def draw_side(items: List[Dict[str, Any]], x_start: int) -> tuple[int, int]:
        total_v = sum(int(x.get("value", 0)) for x in items)
        total_r = sum(int(x.get("rap", 0)) for x in items)
        for idx, it in enumerate(items[:4]):
            sx = x_start + idx * 82
            sy = 52
            draw.rounded_rectangle([sx, sy, sx + 74, sy + 74], radius=8, fill=slot_color)
            thumb = _fetch_thumb(it.get("thumbnail_url", ""), (66, 66))
            if thumb:
                img.paste(thumb, (sx + 4, sy + 4), thumb)
            else:
                # Wrap full item name cleanly inside the slot without ugly truncation
                full_name = str(it.get("name", "Item"))
                wrapped = "\n".join(textwrap.wrap(full_name, width=9)[:3])
                draw.multiline_text((sx + 6, sy + 14), wrapped, fill=text_white, font=font_small, spacing=2)

        draw.text((x_start, 144), "Value", fill=text_muted, font=font_header)
        draw.text((x_start + 64, 144), f"{total_v:,}", fill=blue_val, font=font_mono)
        draw.text((x_start, 166), "RAP", fill=text_muted, font=font_header)
        draw.text((x_start + 64, 166), f"{total_r:,}", fill=green_rap, font=font_mono)
        return total_v, total_r

    off_v, off_r = draw_side(offer_items, 28)
    req_v, req_r = draw_side(request_items, 396)

    if tags:
        draw.text((396, 192), f"Tags: {', '.join(tags[:4])}", fill=text_white, font=font_small)

    val_op = req_v - off_v
    rap_op = req_r - off_r
    op_color = green_rap if val_op >= 0 else red_op
    draw.text(
        (215, 232),
        f"Value OP {val_op:+,}      RAP OP {rap_op:+,}",
        fill=op_color,
        font=font_footer,
    )
    draw.text((525, 254), "Void Local | Market Tools", fill=text_muted, font=font_small)

    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()
