import difflib
import io
import json
import re
import time
import urllib.request
from typing import Any, Dict, List, Optional, Tuple
from db import Database
from proofs import find_item_by_query

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    "Accept": "application/json",
}

COMMENTARY_PATTERNS = re.compile(
    r"(?i)\b(?:used\s+to\s+be|at\s+the\s+time|was\s+worth|was\s+\d|before\s+drop|before\s+raise|"
    r"ignore\s+the|old\s+proof|when\s+it\s+was|valued\s+at\s+the\s+time|note\s*:|btw|back\s+when|"
    r"dropped|raised|projected|proj\s+at)\b"
)

STOPWORDS = {
    "it", "to", "be", "at", "the", "time", "used", "was", "in", "on", "of", "for", "and",
    "or", "vs", "v", "op", "lb", "lowball", "overpay", "underpay", "even", "item", "items",
    "value", "values", "sender", "receiver", "date", "sent", "rec", "user", "with", "trade",
    "from", "old", "new", "proof", "btw", "note", "adds", "add", "robux", "rap", "clean",
    "total", "rolimons", "demand", "rating", "history", "analyze", "offered", "after", "fee",
    "you", "gave", "received", "demo", "key", "testing", "only", "production", "free", "api",
    "trades", "completed", "closed", "r$", "robux",
}

SLANG_ALIASES: Dict[str, str] = {
    "amethyst homies": "cw ultimate: amethyst addiction",
    "ame homies": "cw ultimate: amethyst addiction",
    "ruby homies": "cw ultimate: ruby rapture",
    "sapphire homies": "cw ultimate: sapphire serenity",
    "emerald homies": "cw ultimate: emerald envy",
    "kodachi": "twin kodachi",
    "tabby": "8-bit tabby cat",
    "steelshanks": "captain steelshanks recruiting staff",
    "obc": "outrageous builders club hard hat",
    "legit": "perfectly legitimate business hat",
    "supa": "supa fly cap",
    "ghos": "ghosdeeri",
    "pois": "poisoned horns of the toxic wasteland",
    "vesp": "dominus vespertilio",
    "noir": "noir periastron psi",
    "alpaca": "from the vault: alpaca plushie",
    "8brc": "8-bit royal crown",
}


def _parse_val_token(tok: str) -> Optional[int]:
    t = tok.strip().lower().replace(",", "").replace(" ", "")
    m = re.match(r"^(\d+(?:\.\d+)?)\s*([km])?$", t)
    if not m:
        return None
    num = float(m.group(1))
    mult = m.group(2)
    if mult == "m":
        return int(round(num * 1_000_000))
    if mult == "k":
        return int(round(num * 1_000))
    if num < 1000:
        return int(round(num * 1_000))
    return int(round(num))


def _preprocess_image_for_ocr(image_bytes: bytes) -> List[bytes]:
    variants: List[bytes] = []
    try:
        from PIL import Image, ImageEnhance, ImageFilter, ImageOps

        orig = Image.open(io.BytesIO(image_bytes)).convert("RGB")
        w, h = orig.size
        scale = 2 if max(w, h) < 1600 else 1
        if scale > 1:
            up = orig.resize((w * scale, h * scale), Image.Resampling.LANCZOS)
        else:
            up = orig

        gray = ImageOps.grayscale(up)
        norm = ImageOps.autocontrast(gray, cutoff=2)
        sharp = norm.filter(ImageFilter.UnsharpMask(radius=1.6, percent=175, threshold=3))
        sharp = ImageEnhance.Contrast(sharp).enhance(1.35)

        buf1 = io.BytesIO()
        sharp.save(buf1, format="PNG")
        variants.append(buf1.getvalue())

        mean_luma = sum(norm.getdata()) / max(1, (norm.size[0] * norm.size[1]))
        if mean_luma > 170:
            inv = ImageOps.invert(norm)
            inv_sharp = ImageEnhance.Contrast(inv).enhance(1.5)
            buf2 = io.BytesIO()
            inv_sharp.save(buf2, format="PNG")
            variants.append(buf2.getvalue())
    except Exception:
        pass

    variants.append(image_bytes)
    return variants


def _call_ocr_space(img_bytes: bytes, engine: int = 2) -> str:
    try:
        boundary = "----CarbonOCRBoundary7MA4YWxkTrZu0gW"
        body = (
            f"--{boundary}\r\n"
            f'Content-Disposition: form-data; name="apikey"\r\n\r\nhelloworld\r\n'
            f"--{boundary}\r\n"
            f'Content-Disposition: form-data; name="language"\r\n\r\neng\r\n'
            f"--{boundary}\r\n"
            f'Content-Disposition: form-data; name="scale"\r\n\r\ntrue\r\n'
            f"--{boundary}\r\n"
            f'Content-Disposition: form-data; name="OCREngine"\r\n\r\n{engine}\r\n'
            f"--{boundary}\r\n"
            f'Content-Disposition: form-data; name="file"; filename="trade.png"\r\n'
            f"Content-Type: image/png\r\n\r\n"
        ).encode("utf-8") + img_bytes + f"\r\n--{boundary}--\r\n".encode("utf-8")
        req = urllib.request.Request(
            "https://api.ocr.space/parse/image",
            data=body,
            headers={
                "Content-Type": f"multipart/form-data; boundary={boundary}",
                "User-Agent": "Mozilla/5.0",
            },
        )
        with urllib.request.urlopen(req, timeout=14) as resp:
            payload = json.loads(resp.read().decode("utf-8", errors="replace"))
            parsed_res = payload.get("ParsedResults") or []
            if parsed_res:
                raw_txt = str(parsed_res[0].get("ParsedText") or "")
                return re.sub(r"(?im)^\[note:.*?\]\s*", "", raw_txt).strip()
    except Exception:
        pass
    return ""


def _call_rapidocr_boxes(img_bytes: bytes) -> List[Tuple[List[float], str]]:
    try:
        from rapidocr_onnxruntime import RapidOCR

        global _RAPID_ENGINE
        if "_RAPID_ENGINE" not in globals() or _RAPID_ENGINE is None:
            _RAPID_ENGINE = RapidOCR()
        res, _ = _RAPID_ENGINE(img_bytes)
        if res:
            parsed = []
            for line in res:
                box = line[0]
                text = str(line[1]).strip()
                xs = [pt[0] for pt in box]
                ys = [pt[1] for pt in box]
                parsed.append(([min(xs), max(xs), min(ys), max(ys)], text))
            return parsed
    except Exception:
        pass
    return []


def _extract_items_from_boxes(
    boxes: List[Tuple[List[float], str]], items_catalog: Dict[int, Dict[str, Any]]
) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    gave_y = None
    recv_y = None
    for b, t in boxes:
        tl = re.sub(r"\s+", "", t.lower())
        if "itemsyougave" in tl:
            gave_y = b[2]
        elif "itemsyoureceived" in tl:
            recv_y = b[2]

    if gave_y is None or recv_y is None:
        return [], []

    def cluster_side(lines: List[Tuple[List[float], str]]) -> List[Dict[str, Any]]:
        slots: List[Dict[str, Any]] = []
        for box, text in lines:
            tl = text.lower()
            if any(skip in tl for skip in ("total value", "totalvalue", "robux offered", "robuxoffered", "items you", "itemsyou", "trade with", "tradewith")):
                continue
            if re.match(r"^[米茶#\d\s\W]+$", text) or re.match(r"^[QO0≥]?\s*[\d,.]+$", text):
                continue
            if re.search(r"[-+]\d+.*?(?:rap|value)", tl):
                continue
            cx = (box[0] + box[1]) / 2.0
            cy = (box[2] + box[3]) / 2.0
            placed = False
            for s in slots:
                if abs(s["cx"] - cx) < 85 and abs(s["cy"] - cy) < 130:
                    s["lines"].append((cy, text))
                    s["cy"] = (s["cy"] + cy) / 2.0
                    placed = True
                    break
            if not placed:
                slots.append({"cx": cx, "cy": cy, "lines": [(cy, text)]})
        matched: List[Dict[str, Any]] = []
        for s in slots:
            s["lines"].sort(key=lambda x: x[0])
            comb = " ".join(l[1] for l in s["lines"]).strip()
            comb_spaced = re.sub(r"([a-z])([A-Z])", r"\1 \2", comb)
            it = _strict_item_match(comb_spaced, items_catalog)
            if not it:
                it = _strict_item_match(comb, items_catalog)
            if it and len(matched) < 8:
                matched.append(it)
        return matched

    gave_lines = [item for item in boxes if gave_y < item[0][2] < recv_y]
    recv_lines = [item for item in boxes if item[0][2] > recv_y]
    return cluster_side(gave_lines), cluster_side(recv_lines)


def _call_rapidocr_local(img_bytes: bytes) -> str:
    try:
        from rapidocr_onnxruntime import RapidOCR

        global _RAPID_ENGINE
        if "_RAPID_ENGINE" not in globals() or _RAPID_ENGINE is None:
            _RAPID_ENGINE = RapidOCR()
        res, _ = _RAPID_ENGINE(img_bytes)
        if res:
            return "\n".join(str(line[1]) for line in res if len(line) > 1).strip()
    except Exception:
        pass
    return ""


def ocr_trade_screenshot_bytes(image_bytes: bytes) -> str:
    local_txt = _call_rapidocr_local(image_bytes)
    if len(local_txt) >= 15:
        return local_txt

    variants = _preprocess_image_for_ocr(image_bytes)
    for v in variants[:1]:
        local_v = _call_rapidocr_local(v)
        if len(local_v) >= 15:
            return local_v

    try:
        from PIL import Image
        import pytesseract

        for v in variants:
            img = Image.open(io.BytesIO(v))
            txt = str(pytesseract.image_to_string(img) or "").strip()
            if len(txt) >= 20:
                return txt
    except Exception:
        pass

    for v in variants[:2]:
        txt = _call_ocr_space(v, engine=2).strip()
        if len(txt) >= 15:
            return txt
    return _call_ocr_space(image_bytes, engine=1).strip()


def _strict_item_match(
    query: str, items_catalog: Dict[int, Dict[str, Any]]
) -> Optional[Dict[str, Any]]:
    q = re.sub(r"(?i)\br\$\b", " ", query)
    q = re.sub(r"[^a-zA-Z0-9\s\-':!.]", " ", q).strip().lower()
    q = re.sub(r"\s+", " ", q)
    if not q or q in STOPWORDS or len(q) < 2:
        return None
    if re.match(r"^\d+[km]?$", q):
        return None
    if COMMENTARY_PATTERNS.search(q):
        return None

    if q in SLANG_ALIASES:
        q = SLANG_ALIASES[q]

    for it in items_catalog.values():
        acr = (it.get("acronym") or "").strip().lower()
        if acr and acr == q:
            return it

    for it in items_catalog.values():
        nm = it["name"].strip().lower()
        if nm == q:
            return it

    if len(q) >= 3:
        prefix_hits = []
        q_words = [w for w in re.split(r"[\s\-]+", q) if w and w not in STOPWORDS]
        for it in items_catalog.values():
            nm = it["name"].strip().lower()
            words = re.split(r"[\s\-]+", nm)
            if nm.startswith(q):
                prefix_hits.append((0, -int(it.get("value", 0)), len(nm), it))
            elif len(q_words) == 1 and any(
                w == q or (len(q) >= 4 and w.startswith(q)) for w in words
            ):
                prefix_hits.append((1, -int(it.get("value", 0)), len(nm), it))
            elif len(q_words) >= 2 and all(any(w.startswith(qw) for w in words) for qw in q_words):
                prefix_hits.append((0, -int(it.get("value", 0)), len(nm), it))
        if prefix_hits:
            prefix_hits.sort(key=lambda x: (x[0], x[1], x[2]))
            return prefix_hits[0][3]

    if len(q) >= 5:
        best_it = None
        best_ratio = 0.0
        for it in items_catalog.values():
            nm = it["name"].strip().lower()
            r = difflib.SequenceMatcher(None, q, nm[: max(len(q), 16)]).ratio()
            if r > best_ratio and r >= 0.82:
                best_ratio = r
                best_it = it
        if best_it:
            return best_it

    return None


def _extract_items_from_segment(
    seg: str, items_catalog: Dict[int, Dict[str, Any]]
) -> List[Dict[str, Any]]:
    cleaned = re.sub(r"(?i)^items?(?:\(s\))?\s*:\s*", "", seg).strip()
    cleaned = re.sub(r"(?i)\br\$\b", "", cleaned).strip()
    if not cleaned:
        return []

    found_items: List[Dict[str, Any]] = []

    def _add_item(it: Dict[str, Any], mult: int = 1) -> None:
        existing_cnt = sum(1 for x in found_items if x["item_id"] == it["item_id"])
        target_cnt = max(1, min(4, mult))
        while existing_cnt < target_cnt and len(found_items) < 8:
            found_items.append(it)
            existing_cnt += 1

    mult_map: Dict[int, int] = {}
    for m_mult in re.finditer(r"(?i)\b([234])\s*x\s*([a-zA-Z0-9\s\-':!]+?)(?=\(|,|$|\b[234]x\b)", cleaned):
        cnt = int(m_mult.group(1))
        sub_it = _strict_item_match(m_mult.group(2).strip(), items_catalog)
        if sub_it:
            mult_map[sub_it["item_id"]] = cnt

    cleaned_no_paren = re.sub(
        r"([a-zA-Z0-9\s\-':!]+?)\s*\(\s*([^)]+)\s*\)",
        lambda m: (
            f" , {m.group(1).strip()} , "
            if _strict_item_match(m.group(1).strip(), items_catalog)
            else f" , {m.group(2).strip()} , "
        ),
        cleaned,
    )
    no_parens = re.sub(r"\([^)]*\)", ",", cleaned_no_paren)
    parts = [x.strip() for x in re.split(r"[,+&\n/]+", no_parens) if x.strip()]
    for part in parts:
        m_m = re.match(r"(?i)^([234])\s*x\s+(.+)$", part)
        mult = int(m_m.group(1)) if m_m else 1
        q_part = m_m.group(2).strip() if m_m else part
        it = _strict_item_match(q_part, items_catalog)
        if it:
            _add_item(it, max(mult, mult_map.get(it["item_id"], 1)))

    if len(found_items) == 0 and not COMMENTARY_PATTERNS.search(no_parens):
        norm_seg = re.sub(r"\s+", " ", no_parens.lower())
        for sl_k, sl_v in SLANG_ALIASES.items():
            if sl_k in norm_seg:
                it = _strict_item_match(sl_v, items_catalog)
                if it:
                    _add_item(it, mult_map.get(it["item_id"], 1))
        for it in sorted(
            items_catalog.values(),
            key=lambda x: (len(x["name"]), int(x.get("value", 0))),
            reverse=True,
        ):
            nm = it["name"].lower().strip()
            if len(nm) >= 6 and nm in norm_seg and not any(
                nm in x["name"].lower() for x in found_items
            ):
                _add_item(it, mult_map.get(it["item_id"], 1))
                if len(found_items) >= 8:
                    break

    return found_items[:8]


def _find_all_fuzzy_section_splits(ocr_text: str) -> List[Tuple[str, str]]:
    lines = ocr_text.splitlines()
    markers: List[Tuple[int, str]] = []
    for idx, ln in enumerate(lines):
        clean = re.sub(r"[^a-zA-Z\s]", "", ln).strip().lower()
        clean = re.sub(r"\s+", " ", clean)
        if not clean:
            continue
        score_gave = max(
            1.0 if any(k in clean for k in ("you gave", "you gove", "yau gave", "items you gave")) else 0.0,
            difflib.SequenceMatcher(None, clean, "items you gave").ratio(),
            difflib.SequenceMatcher(None, clean[-8:], "you gave").ratio(),
        )
        score_recv = max(
            1.0 if any(k in clean for k in ("you received", "you rece", "items you received")) else 0.0,
            difflib.SequenceMatcher(None, clean, "items you received").ratio(),
            difflib.SequenceMatcher(None, clean[-12:], "you received").ratio(),
        )
        if score_gave >= 0.70 and score_gave > score_recv:
            markers.append((idx, "GAVE"))
        elif score_recv >= 0.70 and score_recv > score_gave:
            markers.append((idx, "RECV"))

    if not markers:
        return []

    gave_chunks: List[str] = []
    recv_chunks: List[str] = []
    for i, (line_idx, m_type) in enumerate(markers):
        next_idx = markers[i + 1][0] if i + 1 < len(markers) else len(lines)
        chunk = "\n".join(lines[line_idx + 1 : next_idx])
        if m_type == "GAVE":
            gave_chunks.append(chunk)
        else:
            recv_chunks.append(chunk)

    return [("\n".join(gave_chunks), "\n".join(recv_chunks))]


def _extract_items_from_ocr_text(
    ocr_text: str, items_catalog: Dict[int, Dict[str, Any]]
) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    if not ocr_text or not ocr_text.strip():
        return [], []

    splits = _find_all_fuzzy_section_splits(ocr_text)
    if not splits:
        return [], []
    gave_block, recv_block = splits[0]

    catalog_sorted = sorted(
        items_catalog.values(),
        key=lambda x: (len(x["name"]), int(x.get("value", 0))),
        reverse=True,
    )

    def scan_block(block: str) -> List[Dict[str, Any]]:
        if not block.strip():
            return []
        matched: List[Dict[str, Any]] = []
        seen_ids = set()

        b_norm = block.lower()
        b_norm = re.sub(r"(?i)theclassicroblox", "the classic roblox", b_norm)
        b_norm = re.sub(r"[\r\n]+", " ", b_norm)
        b_norm = re.sub(r"[^a-zA-Z0-9\s:!]", " ", b_norm)
        b_norm = re.sub(r"\s+", " ", b_norm).strip()

        for it in catalog_sorted:
            nm = it["name"].lower().strip()
            if len(nm) >= 3:
                pattern = r"\b" + re.escape(nm) + r"(?:\b|(?=[!]))"
                if re.search(pattern, b_norm):
                    count = min(4, len(re.findall(pattern, b_norm)))
                    if it["item_id"] not in seen_ids:
                        seen_ids.add(it["item_id"])
                        for _ in range(max(1, count)):
                            if len(matched) < 8:
                                matched.append(it)

        raw_lines = [
            re.sub(r"[^a-zA-Z0-9\s\-':!]", "", ln).strip().lower()
            for ln in block.splitlines()
        ]
        candidate_lines = [
            ln
            for ln in raw_lines
            if 3 <= len(ln) <= 38
            and not any(
                skip in ln
                for skip in (
                    "items you",
                    "total val",
                    "totel val",
                    "rolimons",
                    "demand",
                    "robux",
                    "robus",
                    "trade with",
                    "analyze",
                    "anayze",
                    "history",
                    "halery",
                )
            )
            and not re.match(r"^[\d\s,.+%/-]+$", ln)
            and any(c.isalpha() for c in ln)
        ]

        window_phrases = list(candidate_lines)
        for i in range(len(candidate_lines) - 1):
            window_phrases.append(f"{candidate_lines[i]} {candidate_lines[i + 1]}")

        for phrase in window_phrases:
            if len(matched) >= 8:
                break
            clean_p = re.sub(r"\s+", " ", phrase).strip()
            if len(clean_p) < 5 or clean_p in STOPWORDS:
                continue
            p_first = clean_p.split()[0]
            best_it = None
            best_score = 0.0
            for it in catalog_sorted:
                if it["item_id"] in seen_ids:
                    continue
                nm = it["name"].lower().strip()
                nm_first = nm.split()[0]
                if difflib.SequenceMatcher(None, p_first, nm_first).ratio() < 0.60:
                    continue
                cmp_target = nm[: min(len(nm), len(clean_p) + 2)] if len(nm) > len(clean_p) + 2 else nm
                score = difflib.SequenceMatcher(None, clean_p, cmp_target).ratio()
                if score > best_score and score >= 0.72:
                    best_score = score
                    best_it = it
            if best_it and best_it["item_id"] not in seen_ids:
                seen_ids.add(best_it["item_id"])
                matched.append(best_it)

        return matched[:8]

    return scan_block(gave_block), scan_block(recv_block)


def parse_value_request_text(
    text: str,
    items_catalog: Dict[int, Dict[str, Any]],
    image_bytes: Optional[bytes] = None,
    extra_images: Optional[List[bytes]] = None,
) -> Optional[Dict[str, Any]]:
    raw_lines = [ln.strip() for ln in (text or "").splitlines() if ln.strip()]
    all_imgs: List[bytes] = []
    if image_bytes:
        all_imgs.append(image_bytes)
    if extra_images:
        all_imgs.extend(extra_images)

    sender = "Sender"
    receiver = "Receiver"
    val_a: Optional[int] = None
    val_b: Optional[int] = None
    explicit_diff: Optional[int] = None
    item_lines: List[str] = []

    num_pat = r"(\d{1,3}(?:[,\s]\d{3})+|\d+(?:\.\d+)?\s*[km]?)"
    val_regex = re.compile(
        rf"(?i)^(?:values?|vals?|v)?\s*:?\s*{num_pat}\s*(?:vs\.?|v\.?|/|-)\s*{num_pat}"
        r"(?:\s*\(([^)]*)\))?"
    )

    for ln in raw_lines:
        m_send = re.match(r"(?i)^(?:sender|sent|send|s)\s*[:=-]\s*@?([^\s(]+)", ln)
        if m_send:
            sender = m_send.group(1).strip()
            continue

        m_recv = re.match(r"(?i)^(?:receiver|received|rec|r)\s*[:=-]\s*@?([^\s(]+)", ln)
        if m_recv:
            receiver = m_recv.group(1).strip()
            continue

        if re.match(r"(?i)^(?:date|d)\s*[:=-]\s*(.+)$", ln):
            continue

        if COMMENTARY_PATTERNS.search(ln) and not re.match(r"(?i)^items?(?:\(s\))?\s*:", ln):
            continue

        vm = val_regex.search(ln)
        if vm and not COMMENTARY_PATTERNS.search(ln):
            va = _parse_val_token(vm.group(1))
            vb = _parse_val_token(vm.group(2))
            if va and vb:
                val_a, val_b = va, vb
                paren_body = (vm.group(3) or "").strip()
                if paren_body:
                    if "even" in paren_body.lower():
                        explicit_diff = 0
                    else:
                        m_op_num = re.search(
                            r"(\d{1,3}(?:[,\s]\d{3})+|\d+(?:\.\d+)?\s*[km]?)\s*(?:[a-zA-Z0-9_']+\s+)?(?:op|overpay|lb|lowball|underpay)",
                            paren_body,
                            flags=re.IGNORECASE,
                        )
                        if m_op_num:
                            explicit_diff = _parse_val_token(m_op_num.group(1))
                continue

        item_lines.append(ln)

    full_item_str = re.sub(r"(?i)^items?(?:\(s\))?\s*:\s*", "", " ".join(item_lines)).strip()
    side_a_items: List[Dict[str, Any]] = []
    side_b_items: List[Dict[str, Any]] = []

    vs_split = re.split(
        r"\s+(?:vs\.?|v\.?|//)\s+", full_item_str, maxsplit=1, flags=re.IGNORECASE
    )
    if len(vs_split) == 2:
        side_a_items = _extract_items_from_segment(vs_split[0], items_catalog)
        side_b_items = _extract_items_from_segment(vs_split[1], items_catalog)
    else:
        slash_parts = [p.strip() for p in re.split(r"(?<!\d)/(?!\d)", full_item_str) if p.strip()]
        if len(slash_parts) == 2:
            side_a_items = _extract_items_from_segment(slash_parts[0], items_catalog)
            side_b_items = _extract_items_from_segment(slash_parts[1], items_catalog)
        elif len(slash_parts) > 2:
            all_slash_items: List[Dict[str, Any]] = []
            for sp in slash_parts:
                all_slash_items.extend(_extract_items_from_segment(sp, items_catalog))
            if len(all_slash_items) >= 2:
                if val_a and val_b:
                    n_it = len(all_slash_items)
                    best_split = None
                    best_err = float("inf")
                    for mask in range(1, (1 << n_it) - 1):
                        sub_a = [all_slash_items[i] for i in range(n_it) if (mask & (1 << i))]
                        sub_b = [all_slash_items[i] for i in range(n_it) if not (mask & (1 << i))]
                        sum_a = sum(int(x.get("value", 0)) for x in sub_a)
                        sum_b = sum(int(x.get("value", 0)) for x in sub_b)
                        err = abs(sum_a - val_a) + abs(sum_b - val_b)
                        if err < best_err:
                            best_err = err
                            best_split = (sub_a, sub_b)
                    if best_split:
                        side_a_items, side_b_items = best_split
                    else:
                        side_a_items = [all_slash_items[0]]
                        side_b_items = all_slash_items[1:]
                else:
                    side_a_items = [all_slash_items[0]]
                    side_b_items = all_slash_items[1:]
        else:
            all_extracted = _extract_items_from_segment(full_item_str, items_catalog)
            if len(all_extracted) >= 2:
                side_a_items = [all_extracted[0]]
                side_b_items = all_extracted[1:]
            elif len(all_extracted) == 1:
                side_a_items = [all_extracted[0]]

    if len(side_a_items) == 1 and val_a and int(side_a_items[0].get("value", 0)) > 0:
        it0 = side_a_items[0]
        v0 = int(it0["value"])
        mult = int(round(val_a / v0))
        if 2 <= mult <= 4 and abs(mult * v0 - val_a) <= v0 * 0.15:
            side_a_items = [it0] * mult

    if len(side_b_items) == 1 and val_b and int(side_b_items[0].get("value", 0)) > 0:
        it0 = side_b_items[0]
        v0 = int(it0["value"])
        mult = int(round(val_b / v0))
        if 2 <= mult <= 4 and abs(mult * v0 - val_b) <= v0 * 0.15:
            side_b_items = [it0] * mult

    txt_val_a = sum(int(x.get("value", 0)) for x in side_a_items)
    txt_val_b = sum(int(x.get("value", 0)) for x in side_b_items)
    text_complete = (
        bool(side_a_items)
        and bool(side_b_items)
        and val_a is not None
        and val_b is not None
        and abs(txt_val_a - val_a) <= max(5000, val_a * 0.03)
        and abs(txt_val_b - val_b) <= max(5000, val_b * 0.03)
    )

    if all_imgs:
        for b in all_imgs:
            if not b:
                continue
            boxes = _call_rapidocr_boxes(b)
            if boxes:
                box_a, box_b = _extract_items_from_boxes(boxes, items_catalog)
                if box_a or box_b:
                    if box_a:
                        side_a_items = box_a
                    if box_b:
                        side_b_items = box_b
                    break

    ocr_text = ""
    if (not side_a_items or not side_b_items) and all_imgs and not text_complete:
        ocr_chunks = [ocr_trade_screenshot_bytes(b) for b in all_imgs if b]
        ocr_text = "\n".join(c for c in ocr_chunks if c)

    if ocr_text:
        ocr_a, ocr_b = _extract_items_from_ocr_text(ocr_text, items_catalog)
        if ocr_a or ocr_b:
            a_ids = {x["item_id"] for x in side_a_items}
            b_ids = {x["item_id"] for x in side_b_items}
            ocr_a_ids = {x["item_id"] for x in ocr_a}
            ocr_b_ids = {x["item_id"] for x in ocr_b}
            if (a_ids & ocr_b_ids) or (b_ids & ocr_a_ids):
                ocr_a, ocr_b = ocr_b, ocr_a

            def _merge_side(txt_side: List[Dict[str, Any]], ocr_side: List[Dict[str, Any]], opp_ids: set) -> List[Dict[str, Any]]:
                merged = list(txt_side)
                ocr_counts: Dict[int, int] = {}
                for x in ocr_side:
                    ocr_counts[x["item_id"]] = ocr_counts.get(x["item_id"], 0) + 1
                for x in ocr_side:
                    iid = x["item_id"]
                    if iid in opp_ids:
                        continue
                    cur_cnt = sum(1 for m in merged if m["item_id"] == iid)
                    target_cnt = ocr_counts.get(iid, 1)
                    if cur_cnt < target_cnt and len(merged) < 8:
                        merged.append(x)
                return merged

            side_a_items = _merge_side(side_a_items, ocr_a, b_ids)
            a_ids_after = {x["item_id"] for x in side_a_items}
            side_b_items = _merge_side(side_b_items, ocr_b, a_ids_after)

        m_with = re.search(r"Trade with\s+([^\s@]+)(?:\s+@(\w+))?", ocr_text)
        if m_with and receiver == "Receiver":
            receiver = m_with.group(2) or m_with.group(1)

    if not side_a_items or not side_b_items:
        return None

    calc_val_a = sum(int(x.get("value", 0)) for x in side_a_items)
    calc_rap_a = sum(int(x.get("rap", 0)) for x in side_a_items)
    calc_val_b = sum(int(x.get("value", 0)) for x in side_b_items)
    calc_rap_b = sum(int(x.get("rap", 0)) for x in side_b_items)

    if val_a is not None and val_b is not None:
        direct_err = abs(calc_val_a - val_a) + abs(calc_val_b - val_b)
        swapped_err = abs(calc_val_a - val_b) + abs(calc_val_b - val_a)
        if swapped_err + 3000 < direct_err:
            val_a, val_b = val_b, val_a

    final_val_a = val_a if val_a is not None else calc_val_a
    final_val_b = val_b if val_b is not None else calc_val_b
    op_val = explicit_diff if explicit_diff is not None else abs(final_val_a - final_val_b)
    op_rap = abs(calc_rap_a - calc_rap_b)

    trade_type = "SWAP"
    if len(side_a_items) > len(side_b_items):
        trade_type = "UPGRADE"
    elif len(side_b_items) > len(side_a_items):
        trade_type = "DOWNGRADE"

    return {
        "sender": sender,
        "receiver": receiver,
        "items_a": side_a_items,
        "items_b": side_b_items,
        "value_a": final_val_a,
        "rap_a": calc_rap_a if calc_rap_a > 0 else final_val_a,
        "value_b": final_val_b,
        "rap_b": calc_rap_b if calc_rap_b > 0 else final_val_b,
        "op_value": op_val,
        "op_rap": op_rap,
        "trade_type": trade_type,
    }


def save_parsed_value_request_proof(
    db: Database, parsed: Dict[str, Any], detected_at: Optional[int] = None
) -> int:
    now = detected_at if detected_at is not None else int(time.time())
    items_a = [dict(x) for x in parsed["items_a"] if int(x.get("item_id", 0)) > 0]
    items_b = [dict(x) for x in parsed["items_b"] if int(x.get("item_id", 0)) > 0]
    if not items_a or not items_b:
        return 0

    return db.insert_trade(
        user_a_id=888002,
        user_a_name=parsed.get("sender", "Sender"),
        user_b_id=888003,
        user_b_name=parsed.get("receiver", "Receiver"),
        items_a=items_a,
        items_b=items_b,
        value_a=int(parsed["value_a"]),
        rap_a=int(parsed["rap_a"]),
        value_b=int(parsed["value_b"]),
        rap_b=int(parsed["rap_b"]),
        op_value=int(parsed["op_value"]),
        op_rap=int(parsed["op_rap"]),
        trade_type=str(parsed.get("trade_type", "SWAP")),
        detected_at=now,
    )


def poll_discord_value_requests_channel(
    db: Database,
    items_catalog: Dict[int, Dict[str, Any]],
    channel_id: str,
    auth_token: str,
    seen_message_ids: set[str],
) -> int:
    if not channel_id or not auth_token:
        return 0
    url = f"https://discord.com/api/v10/channels/{channel_id.strip()}/messages?limit=15"
    hdrs = {
        "Authorization": auth_token.strip(),
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
        "Accept": "application/json",
    }
    is_initial = len(seen_message_ids) == 0
    pages = 10 if is_initial else 1
    messages: List[Dict[str, Any]] = []
    before_id: Optional[str] = None
    for _ in range(pages):
        page_url = (
            f"https://discord.com/api/v10/channels/{channel_id.strip()}/messages?limit={'100' if is_initial else '15'}"
            + (f"&before={before_id}" if before_id else "")
        )
        try:
            req = urllib.request.Request(page_url, headers=hdrs)
            with urllib.request.urlopen(req, timeout=10) as resp:
                batch = json.loads(resp.read().decode("utf-8", errors="replace"))
            if not isinstance(batch, list) or not batch:
                break
            messages.extend(batch)
            before_id = str(batch[-1].get("id", ""))
            if is_initial:
                time.sleep(0.4)
        except Exception:
            break

    if not messages:
        return 0

    imported = 0
    ocr_calls = 0
    for msg in reversed(messages):
        mid = str(msg.get("id", ""))
        if not mid or mid in seen_message_ids:
            continue
        seen_message_ids.add(mid)
        content = str(msg.get("content") or "")
        parsed = parse_value_request_text(content, items_catalog)
        val_sum_a = sum(int(x.get("value", 0)) for x in parsed["items_a"]) if parsed else 0
        val_sum_b = sum(int(x.get("value", 0)) for x in parsed["items_b"]) if parsed else 0
        needs_ocr = (
            not parsed
            or abs(val_sum_a - int(parsed["value_a"])) > 5000
            or abs(val_sum_b - int(parsed["value_b"])) > 5000
        )
        if needs_ocr and ocr_calls < 5:
            attachments = msg.get("attachments") or []
            img_list: List[bytes] = []
            for att in attachments[:2]:
                att_url = str(att.get("url") or "")
                if att_url:
                    try:
                        ireq = urllib.request.Request(att_url, headers={"User-Agent": "Mozilla/5.0"})
                        with urllib.request.urlopen(ireq, timeout=6) as iresp:
                            img_list.append(iresp.read())
                    except Exception:
                        pass
            if img_list:
                ocr_calls += 1
                parsed_ocr = parse_value_request_text(
                    content,
                    items_catalog,
                    image_bytes=img_list[0],
                    extra_images=img_list[1:] if len(img_list) > 1 else None,
                )
                if parsed_ocr:
                    parsed = parsed_ocr
        if parsed:
            tid = save_parsed_value_request_proof(db, parsed)
            if tid > 0:
                imported += 1
    return imported
