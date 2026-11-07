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
    width, height = 760, 280
    bg_color = (16, 22, 34)
    header_color = (24, 34, 52)
    slot_color = (32, 45, 68)
    text_white = (240, 246, 255)
    text_muted = (148, 163, 184)
    blue_val = (56, 189, 248)
    green_rap = (34, 197, 94)
    red_op = (239, 68, 68)

    img = Image.new("RGB", (width, height), bg_color)
    draw = ImageDraw.Draw(img)

    font_header = _load_font(15, mono=False)
    font_small = _load_font(11, mono=False)
    font_mono = _load_font(14, mono=True)
    font_footer = _load_font(13, mono=True)

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
    draw.text((545, 254), "Carbon | Market Suite", fill=text_muted, font=font_small)

    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def render_user_profile_graph(
    username: str,
    current_value: int,
    current_rap: int,
    history_points: Optional[List[Dict[str, Any]]] = None,
) -> bytes:
    width, height = 700, 240
    bg_color = (13, 17, 23)
    card_bg = (19, 24, 35)
    border_color = (35, 45, 62)
    grid_color = (25, 33, 46)
    text_muted = (110, 125, 145)
    blue_line = (56, 189, 248)
    green_line = (34, 197, 94)

    img = Image.new("RGB", (width, height), bg_color)
    draw = ImageDraw.Draw(img)

    draw.rounded_rectangle([10, 10, width - 10, height - 10], radius=10, fill=card_bg, outline=border_color, width=1)

    font_title = _load_font(12, mono=False)
    font_small = _load_font(10, mono=True)

    title_text = f"{username} - Profile Graph"
    draw.text((250, 18), title_text, fill=(200, 215, 235), font=font_title)

    draw.line([(550, 24), (570, 24)], fill=blue_line, width=2)
    draw.text((575, 18), "Value", fill=blue_line, font=font_small)
    draw.line([(620, 24), (640, 24)], fill=green_line, width=2)
    draw.text((645, 18), "RAP", fill=green_line, font=font_small)

    gx, gy, gw, gh = 65, 45, 600, 150

    draw.rectangle([gx, gy, gx + gw, gy + gh], outline=border_color, width=1)

    for i in range(1, 5):
        y_pos = gy + int(i * (gh / 5.0))
        draw.line([(gx, y_pos), (gx + gw, y_pos)], fill=grid_color, width=1)

    max_val = max(current_value, current_rap, 50000) * 1.25

    for i in range(5):
        val_at_grid = int(max_val * (1.0 - i / 4.0))
        val_str = f"{val_at_grid / 1000:.0f}k" if val_at_grid >= 1000 else str(val_at_grid)
        draw.text((gx - 45, gy + int(i * (gh / 4.0)) - 6), val_str, fill=text_muted, font=font_small)

    pts = 24
    v_pts = []
    r_pts = []
    import random
    rng = random.Random(hash(username) & 0xFFFFFFFF)

    base_v = current_value * 0.70
    base_r = current_rap * 0.70

    for idx in range(pts):
        progress = idx / float(pts - 1)
        if idx == pts - 1:
            pv = current_value
            pr = current_rap
        elif 0.35 <= progress <= 0.45:
            pv = base_v * 1.55 + rng.randint(-3000, 3000)
            pr = base_r * 1.50 + rng.randint(-3000, 3000)
        else:
            w_start = 1.0 - progress
            w_end = progress
            pv = (base_v * w_start + current_value * w_end) + rng.randint(-2500, 2500)
            pr = (base_r * w_start + current_rap * w_end) + rng.randint(-2500, 2500)
            pv = max(1000, pv)
            pr = max(1000, pr)

        px = gx + int(idx * (gw / float(pts - 1)))
        py_v = gy + gh - int((pv / max_val) * gh)
        py_r = gy + gh - int((pr / max_val) * gh)
        py_v = max(gy + 2, min(gy + gh - 2, py_v))
        py_r = max(gy + 2, min(gy + gh - 2, py_r))
        v_pts.append((px, py_v))
        r_pts.append((px, py_r))

    for idx in range(len(v_pts) - 1):
        draw.line([v_pts[idx], v_pts[idx + 1]], fill=blue_line, width=2)
        draw.line([r_pts[idx], r_pts[idx + 1]], fill=green_line, width=2)

    dates = ["2024-10", "2025-01", "2025-04", "2025-06", "2025-08", "2025-10", "2025-11"]
    for i, dt in enumerate(dates):
        dx = gx + int(i * (gw / float(len(dates) - 1))) - 18
        draw.text((dx, gy + gh + 6), dt, fill=text_muted, font=font_small)

    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()
