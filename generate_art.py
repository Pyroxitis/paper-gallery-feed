#!/usr/bin/env python3
"""Paper Gallery daily feed generator.

Fetches CC0 Open Access works from the Cleveland Museum of Art (CMA),
selects line-art-friendly works, converts them to 800x480 1-bit e-paper
frames, and publishes a small static daily pack for Paper Gallery.

The ESP32 never decodes museum JPEGs.  It downloads the finished 48,000-byte
1-bpp frame produced by this script.
"""

from __future__ import annotations

import argparse
import binascii
import datetime as dt
import html
import io
import json
import math
import random
import statistics
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import quote

import requests
from PIL import Image, ImageEnhance, ImageFilter, ImageOps, ImageStat
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

API = "https://openaccess-api.clevelandart.org/api/artworks/"
MUSEUM = "Cleveland Museum of Art"
W, H = 800, 480
FRAME_BYTES = W * H // 8

# CMA supports `fields`, so keep search responses small and predictable.
API_FIELDS = ",".join(
    [
        "id",
        "accession_number",
        "share_license_status",
        "title",
        "creation_date",
        "creators",
        "culture",
        "technique",
        "support_materials",
        "department",
        "collection",
        "type",
        "url",
        "images",
    ]
)

SEARCH_TERMS = [
    "etching",
    "engraving",
    "woodcut",
    "wood engraving",
    "pen ink drawing",
    "architectural drawing",
    "botanical illustration",
    "landscape drawing",
    "drypoint",
    "lithograph",
    "natural history illustration",
    "city view print",
]

POSITIVE = {
    "etching": 100,
    "engraving": 100,
    "woodcut": 100,
    "wood engraving": 100,
    "drypoint": 95,
    "pen and ink": 92,
    "pen & ink": 92,
    "ink": 65,
    "architectural": 90,
    "botanical": 88,
    "drawing": 72,
    "graphite": 72,
    "print": 50,
    "lithograph": 55,
    "landscape": 45,
    "natural history": 70,
    "illustration": 60,
}

NEGATIVE = {
    "oil on canvas": -130,
    "oil painting": -130,
    "painting": -90,
    "photograph": -140,
    "photography": -140,
    "sculpture": -180,
    "ceramic": -160,
    "textile": -160,
    "furniture": -160,
    "coin": -150,
    "vessel": -130,
}


@dataclass
class Candidate:
    raw: dict[str, Any]
    score: float
    query: str
    image: Image.Image | None = None
    visual_score: float = 0.0
    mode: str = "atkinson"

    @property
    def id(self) -> str:
        accession = clean_text(self.raw.get("accession_number"))
        object_id = clean_text(self.raw.get("id"), "unknown")
        return f"cma-{accession or object_id}"


def flatten_text(value: object) -> str:
    """Flatten CMA values (including lists/dicts) into searchable text."""
    if value is None:
        return ""
    if isinstance(value, dict):
        return " ".join(flatten_text(v) for v in value.values() if v is not None)
    if isinstance(value, (list, tuple, set)):
        return " ".join(flatten_text(v) for v in value if v is not None)
    return str(value)


def clean_text(value: object, fallback: str = "") -> str:
    text = flatten_text(value)
    text = " ".join(text.replace("\r", " ").replace("\n", " ").split())
    return text.strip() or fallback


def first_artist(creators: object) -> str:
    if not isinstance(creators, list):
        return ""
    for creator in creators:
        if isinstance(creator, dict):
            artist = clean_text(creator.get("description") or creator.get("name"))
        else:
            artist = clean_text(creator)
        if artist:
            return artist
    return ""


def normalize_artwork(a: dict[str, Any]) -> dict[str, Any] | None:
    """Convert a CMA API artwork record to Paper Gallery's internal schema."""
    if not isinstance(a, dict):
        return None

    images = a.get("images") or {}
    if not isinstance(images, dict):
        images = {}
    web = images.get("web") or {}
    if not isinstance(web, dict):
        web = {}
    image_url = clean_text(web.get("url"))

    license_status = clean_text(a.get("share_license_status")).upper()
    object_id = a.get("id")
    accession = clean_text(a.get("accession_number"))

    # A valid candidate must have an object identity, be explicitly CC0, and
    # expose a direct web image URL.
    if object_id is None or license_status != "CC0" or not image_url:
        return None

    artist = first_artist(a.get("creators"))
    return {
        "id": object_id,
        "accession_number": accession,
        "title": a.get("title"),
        "artist_title": artist,
        "artist_display": artist,
        "date_display": a.get("creation_date"),
        "image_url": image_url,
        "artwork_type_title": a.get("type"),
        "classification_title": a.get("type"),
        "medium_display": a.get("technique"),
        "technique_titles": a.get("technique"),
        "material_titles": a.get("support_materials"),
        "subject_titles": [a.get("department"), a.get("collection"), a.get("culture")],
        "is_public_domain": True,
        "source_url": a.get("url"),
    }


def metadata_blob(a: dict[str, Any]) -> str:
    parts = [
        a.get("title"),
        a.get("artwork_type_title"),
        a.get("classification_title"),
        a.get("medium_display"),
        a.get("technique_titles"),
        a.get("material_titles"),
        a.get("subject_titles"),
    ]
    return clean_text(parts).lower()


def metadata_score(a: dict[str, Any]) -> float:
    if not a.get("is_public_domain") or not a.get("image_url"):
        return -9999.0

    blob = metadata_blob(a)
    score = 0.0
    for term, pts in POSITIVE.items():
        if term in blob:
            score += pts
    for term, pts in NEGATIVE.items():
        if term in blob:
            score += pts

    if "paper" in blob:
        score += 18
    if "black" in blob or "brown ink" in blob:
        score += 12
    return score


def make_session() -> requests.Session:
    """Create a polite, retrying HTTP session for CMA API/CDN requests."""
    retry = Retry(
        total=4,
        connect=4,
        read=4,
        status=4,
        backoff_factor=0.6,
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=frozenset({"GET"}),
        respect_retry_after_header=True,
    )
    adapter = HTTPAdapter(max_retries=retry, pool_connections=4, pool_maxsize=4)
    session = requests.Session()
    session.mount("https://", adapter)
    session.mount("http://", adapter)
    session.headers.update(
        {
            "User-Agent": "PaperGallery/1.2 (+personal e-paper art frame)",
            "Accept": "*/*",
        }
    )
    return session


def api_search(session: requests.Session, query: str, limit: int = 30) -> list[dict[str, Any]]:
    # CMA's docs describe `cc0` as a flag. `cc0=1` is also used by existing
    # CMA API clients and is unambiguous when encoded by requests.
    params = {
        "q": query,
        "cc0": 1,
        "has_image": 1,
        "limit": limit,
        "fields": API_FIELDS,
    }
    r = session.get(API, params=params, timeout=(10, 30))
    r.raise_for_status()
    payload = r.json()

    rows = payload.get("data", []) if isinstance(payload, dict) else []
    if not isinstance(rows, list):
        raise RuntimeError("CMA API returned an unexpected response shape")

    normalized: list[dict[str, Any]] = []
    for row in rows:
        work = normalize_artwork(row)
        if work is not None:
            normalized.append(work)
    return normalized


def gather_candidates(session: requests.Session, seed: int) -> list[Candidate]:
    rng = random.Random(seed)
    terms = SEARCH_TERMS[:]
    rng.shuffle(terms)
    seen: set[str] = set()
    out: list[Candidate] = []

    # Ten modest search requests gives enough variety while staying gentle on
    # the public API. Search-term order and score jitter are deterministic per day.
    for q in terms[:10]:
        try:
            rows = api_search(session, q, 30)
        except (requests.RequestException, ValueError, RuntimeError) as exc:
            print(f"search failed for {q!r}: {exc}")
            continue

        for a in rows:
            object_key = clean_text(a.get("accession_number")) or clean_text(a.get("id"))
            if not object_key or object_key in seen:
                continue
            seen.add(object_key)
            score = metadata_score(a)
            if score > 15:
                # Daily deterministic jitter prevents the same famous works from
                # dominating every pack while retaining quality preference.
                score += rng.uniform(-28, 28)
                out.append(Candidate(a, score, q))

    out.sort(key=lambda c: c.score, reverse=True)
    return out


def download_image(session: requests.Session, c: Candidate) -> Image.Image:
    url = clean_text(c.raw.get("image_url"))
    if not url:
        raise RuntimeError("No CMA web image URL")

    r = session.get(url, timeout=(10, 40), headers={"Accept": "image/*,*/*;q=0.8"})
    r.raise_for_status()
    if not r.content:
        raise RuntimeError("CMA returned an empty image response")

    try:
        im = Image.open(io.BytesIO(r.content))
        im.load()
    except Exception as exc:
        content_type = r.headers.get("Content-Type", "unknown")
        raise RuntimeError(f"Downloaded data is not a readable image ({content_type})") from exc

    im = ImageOps.exif_transpose(im)
    return im.convert("RGB")


def crop_scan_border(gray: Image.Image) -> Image.Image:
    """Trim obvious near-white scanner/mat borders without aggressive cropping."""
    im = gray.convert("L")
    w, h = im.size
    if w < 20 or h < 20:
        return im

    cw = max(4, w // 20)
    ch = max(4, h // 20)
    corners = [
        im.crop((0, 0, cw, ch)),
        im.crop((w - cw, 0, w, ch)),
        im.crop((0, h - ch, cw, h)),
        im.crop((w - cw, h - ch, w, h)),
    ]
    corner_mean = statistics.mean(ImageStat.Stat(x).mean[0] for x in corners)
    if corner_mean < 185:
        return im

    threshold = max(215, min(248, int(corner_mean - 9)))
    # White in this mask means content darker than the paper/mat.
    mask = im.point(lambda p: 255 if p < threshold else 0)
    bbox = mask.getbbox()
    if not bbox:
        return im

    l, t, r, b = bbox
    pad_x = max(3, int((r - l) * 0.025))
    pad_y = max(3, int((b - t) * 0.025))
    box = (
        max(0, l - pad_x),
        max(0, t - pad_y),
        min(w, r + pad_x),
        min(h, b + pad_y),
    )
    cropped = im.crop(box)

    # Reject implausibly aggressive trims (for example, a single dark mark on
    # an otherwise intentionally blank sheet).
    if cropped.width * cropped.height < 0.28 * w * h:
        return im
    return cropped


def contain_on_canvas(gray: Image.Image) -> Image.Image:
    """Fit the complete artwork on an 800x480 white canvas with a small margin."""
    im = gray.convert("L")
    if im.width <= 0 or im.height <= 0:
        raise ValueError("Artwork image has invalid dimensions")

    max_w, max_h = W - 24, H - 24
    scale = min(max_w / im.width, max_h / im.height)
    new_size = (max(1, round(im.width * scale)), max(1, round(im.height * scale)))
    im = im.resize(new_size, Image.Resampling.LANCZOS)

    canvas = Image.new("L", (W, H), 255)
    x = (W - im.width) // 2
    y = (H - im.height) // 2
    canvas.paste(im, (x, y))
    return canvas


def image_metrics(gray: Image.Image) -> dict[str, float]:
    small = gray.convert("L").resize((200, 120), Image.Resampling.BILINEAR)
    stat = ImageStat.Stat(small)
    mean = stat.mean[0]
    std = stat.stddev[0]
    hist = small.histogram()
    n = small.width * small.height
    white = sum(hist[235:]) / n
    dark = sum(hist[:80]) / n
    mid = sum(hist[80:210]) / n
    edges = small.filter(ImageFilter.FIND_EDGES)
    edge_mean = ImageStat.Stat(edges).mean[0]
    return {"mean": mean, "std": std, "white": white, "dark": dark, "mid": mid, "edge": edge_mean}


def visual_score(metrics: dict[str, float]) -> float:
    # Good 1-bit e-paper candidates generally have a light substrate, useful
    # edge structure, and limited solid-black coverage.
    score = 0.0
    score += min(metrics["white"], 0.75) * 80
    score += min(metrics["edge"] / 30.0, 1.5) * 45
    score += min(metrics["std"] / 65.0, 1.5) * 35
    if metrics["dark"] > 0.34:
        score -= (metrics["dark"] - 0.34) * 220
    if metrics["mean"] < 115:
        score -= (115 - metrics["mean"]) * 1.2
    if metrics["white"] < 0.12:
        score -= 35
    return score


def choose_mode(metrics: dict[str, float], blob: str) -> str:
    line_keywords = (
        "architectural",
        "botanical",
        "pen and ink",
        "pen & ink",
        "graphite",
        "drawing",
        "woodcut",
    )
    if any(k in blob for k in line_keywords) and metrics["white"] > 0.28:
        return "line"
    if metrics["mid"] < 0.16 and metrics["white"] > 0.38:
        return "line"
    return "atkinson"


def auto_levels(gray: Image.Image) -> Image.Image:
    # Conservative autocontrast avoids erasing pale hatch marks.
    return ImageOps.autocontrast(gray.convert("L"), cutoff=(0.7, 0.7))


def line_art(gray: Image.Image) -> Image.Image:
    g = auto_levels(gray)
    g = ImageEnhance.Contrast(g).enhance(1.18)
    g = g.filter(ImageFilter.UnsharpMask(radius=0.8, percent=110, threshold=3))

    # Slightly adaptive threshold using a blurred local background estimate.
    local = g.filter(ImageFilter.GaussianBlur(radius=5.0))
    a = g.tobytes()
    b = local.tobytes()
    out = bytearray(len(a))
    for i, (p, bg) in enumerate(zip(a, b)):
        threshold = max(128, min(224, bg - 18))
        out[i] = 255 if p >= threshold else 0
    return Image.frombytes("L", g.size, bytes(out)).convert("1", dither=Image.Dither.NONE)


def atkinson(gray: Image.Image) -> Image.Image:
    g = auto_levels(gray)
    g = ImageEnhance.Contrast(g).enhance(1.08)
    w, h = g.size
    px = [float(v) for v in g.tobytes()]

    # Atkinson dithering: distribute 1/8 of the quantization error to six
    # nearby pixels, preserving crisp highlights well on monochrome e-paper.
    for y in range(h):
        row = y * w
        for x in range(w):
            i = row + x
            old = px[i]
            new = 255.0 if old >= 128.0 else 0.0
            px[i] = new
            error = (old - new) / 8.0
            for dx, dy in ((1, 0), (2, 0), (-1, 1), (0, 1), (1, 1), (0, 2)):
                nx, ny = x + dx, y + dy
                if 0 <= nx < w and 0 <= ny < h:
                    j = ny * w + nx
                    px[j] = min(255.0, max(0.0, px[j] + error))

    data = bytes(255 if v >= 128 else 0 for v in px)
    return Image.frombytes("L", (w, h), data).convert("1", dither=Image.Dither.NONE)


def pack_1bpp(img1: Image.Image) -> bytes:
    """Pack an 800x480 1-bit frame row-major, MSB-first.

    Paper Gallery's feed convention is 1=white/background and 0=black/ink.
    """
    im = img1.convert("1")
    if im.size != (W, H):
        raise ValueError(f"frame must be {W}x{H}, got {im.width}x{im.height}")

    pixels = im.load()
    out = bytearray(FRAME_BYTES)
    k = 0
    for y in range(H):
        for x0 in range(0, W, 8):
            value = 0
            for bit in range(8):
                if pixels[x0 + bit, y] != 0:
                    value |= 1 << (7 - bit)
            out[k] = value
            k += 1
    return bytes(out)


def evaluate_candidates(session: requests.Session, candidates: list[Candidate], count: int) -> list[Candidate]:
    """Download/evaluate enough candidates to reliably fill the requested pack."""
    # Start with strong metadata candidates, but continue deeper if some image
    # downloads fail. Cap work so a transient upstream issue cannot run forever.
    max_to_try = min(len(candidates), max(count * 6, 36))
    evaluated: list[Candidate] = []

    for i, c in enumerate(candidates[:max_to_try]):
        try:
            image = download_image(session, c)
            gray = crop_scan_border(ImageOps.grayscale(image))
            canvas = contain_on_canvas(gray)
            metrics = image_metrics(canvas)
            c.visual_score = visual_score(metrics)
            c.mode = choose_mode(metrics, metadata_blob(c.raw))
            c.score += c.visual_score
            c.image = canvas.convert("RGB")
            evaluated.append(c)
            print(
                f"{i + 1:02d}/{max_to_try} score={c.score:6.1f} "
                f"mode={c.mode:8s} {clean_text(c.raw.get('title'), 'Untitled')}"
            )
        except Exception as exc:
            print(f"image failed for {c.id}: {exc}")

        # Be courteous to the museum CDN without making Actions unnecessarily slow.
        time.sleep(0.35)

        # Once we have a healthy surplus of viable images, further downloads add
        # little value. Keep at least 2x the requested count for final ranking.
        if len(evaluated) >= max(count * 2, count + 6) and i + 1 >= max(count * 3, 24):
            break

    evaluated.sort(key=lambda c: c.score, reverse=True)

    # Diversify artists where possible.
    selected: list[Candidate] = []
    artists: set[str] = set()
    for c in evaluated:
        artist = clean_text(c.raw.get("artist_title") or c.raw.get("artist_display"), "Unknown artist")
        key = artist.casefold()
        if key in artists and len(selected) < max(3, count // 2):
            continue
        selected.append(c)
        artists.add(key)
        if len(selected) == count:
            return selected

    # Fill any remaining slots even if that requires repeated artists.
    for c in evaluated:
        if c not in selected:
            selected.append(c)
            if len(selected) == count:
                break
    return selected


def render_candidate(c: Candidate) -> tuple[Image.Image, bytes]:
    if c.image is None:
        raise RuntimeError(f"candidate {c.id} has no prepared image")
    gray = ImageOps.grayscale(c.image)
    bw = line_art(gray) if c.mode == "line" else atkinson(gray)
    packed = pack_1bpp(bw)
    if len(packed) != FRAME_BYTES:
        raise RuntimeError(f"packed frame is {len(packed)} bytes, expected {FRAME_BYTES}")
    return bw, packed


def source_url(a: dict[str, Any]) -> str:
    src = clean_text(a.get("source_url"))
    if src:
        return src
    accession = clean_text(a.get("accession_number"))
    if accession:
        return f"https://www.clevelandart.org/art/{quote(accession, safe='')}"
    return f"https://www.clevelandart.org/art/{quote(clean_text(a.get('id'), 'unknown'), safe='')}"


def write_slot(out: Path, pack: str, slot: int, count: int, c: Candidate) -> dict[str, Any]:
    bw, packed = render_candidate(c)
    crc = binascii.crc32(packed) & 0xFFFFFFFF
    a = c.raw
    title = clean_text(a.get("title"), "Untitled")
    artist = clean_text(a.get("artist_title") or a.get("artist_display"), "Unknown artist")
    date = clean_text(a.get("date_display"))
    src = source_url(a)

    (out / f"slot{slot}.bin").write_bytes(packed)
    bw.convert("L").save(out / f"slot{slot}.png", optimize=True)

    lines = [
        "PG1",
        f"pack={pack}",
        f"slot={slot}",
        f"count={count}",
        f"id={c.id}",
        f"title={title}",
        f"artist={artist}",
        f"date={date}",
        f"museum={MUSEUM}",
        f"source={src}",
        f"mode={c.mode}",
        f"bytes={len(packed)}",
        f"crc32={crc:08X}",
    ]
    (out / f"slot{slot}.txt").write_text("\n".join(lines) + "\n", encoding="utf-8")

    return {
        "slot": slot,
        "id": c.id,
        "title": title,
        "artist": artist,
        "date": date,
        "museum": MUSEUM,
        "source": src,
        "mode": c.mode,
        "bytes": len(packed),
        "crc32": f"{crc:08X}",
    }


def write_gallery_html(site_dir: Path, pack: str, works: list[dict[str, Any]]) -> None:
    cards: list[str] = []
    for work in works:
        title = html.escape(str(work["title"]), quote=True)
        artist = html.escape(str(work["artist"]), quote=True)
        date = html.escape(str(work["date"]), quote=True)
        mode = html.escape(str(work["mode"]), quote=True)
        source = html.escape(str(work["source"]), quote=True)
        slot = int(work["slot"])
        cards.append(
            f'<article><img src="feed/slot{slot}.png" alt="{title}">'
            f"<h2>{title}</h2><p>{artist}</p>"
            f'<p class="muted">{date} · {mode}</p>'
            f'<a href="{source}" rel="noopener">Museum record</a></article>'
        )

    page = f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Paper Gallery feed</title>
<style>
body{{font-family:system-ui,sans-serif;background:#f3f0e8;color:#171717;margin:0}}
main{{max-width:1100px;margin:auto;padding:30px 18px 60px}}
h1{{font-family:Georgia,serif;font-size:48px;font-weight:500}}
.grid{{display:grid;grid-template-columns:repeat(auto-fit,minmax(280px,1fr));gap:22px}}
article{{background:white;border:1px solid #d4cec1;padding:14px}}
img{{width:100%;height:auto;background:white;border:1px solid #eee}}
h2{{font-family:Georgia,serif;font-weight:500}}
.muted{{color:#716c63}}
a{{color:#171717}}
</style>
</head>
<body><main>
<p>PAPER GALLERY</p>
<h1>Daily public-domain art pack</h1>
<p>Pack {html.escape(pack, quote=True)} · {html.escape(MUSEUM, quote=True)} CC0 Open Access works.</p>
<div class="grid">{''.join(cards)}</div>
</main></body></html>
"""
    (site_dir / "index.html").write_text(page, encoding="utf-8")


def validate_pack_date(date_str: str | None) -> str:
    if date_str is None:
        return dt.date.today().isoformat()
    try:
        return dt.date.fromisoformat(date_str).isoformat()
    except ValueError as exc:
        raise ValueError("--date must be a valid ISO date in YYYY-MM-DD format") from exc


def generate(output: Path, site_dir: Path, count: int, date_str: str | None = None) -> None:
    pack = validate_pack_date(date_str)
    seed = int(pack.replace("-", ""))
    session = make_session()

    candidates = gather_candidates(session, seed)
    if len(candidates) < count:
        raise RuntimeError(
            f"Only {len(candidates)} viable metadata candidates were found; need {count}. "
            "The CMA API may be temporarily unavailable or the search filters may need adjustment."
        )

    selected = evaluate_candidates(session, candidates, count)
    if len(selected) < count:
        raise RuntimeError(
            f"Only {len(selected)} downloadable/usable candidates were found; need {count}. "
            "Check the preceding image-download errors for the upstream cause."
        )

    output.mkdir(parents=True, exist_ok=True)
    site_dir.mkdir(parents=True, exist_ok=True)

    works = [write_slot(output, pack, i, count, c) for i, c in enumerate(selected)]
    (output / "index.txt").write_text(f"PG1\npack={pack}\ncount={count}\n", encoding="utf-8")
    (output / "index.json").write_text(
        json.dumps({"pack": pack, "count": count, "works": works}, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    write_gallery_html(site_dir, pack, works)
    print(f"Wrote {count} artworks to {output}")


def self_test() -> None:
    """Offline tests covering CMA normalization, image processing and packing."""
    fake = {
        "id": 160729,
        "accession_number": "1998.78.14",
        "share_license_status": "CC0",
        "title": "Landscape Study",
        "creation_date": "c. 1800",
        "creators": [{"description": "Example Artist (Test, 1750-1820)"}],
        "culture": ["European"],
        "technique": "etching and drypoint on laid paper",
        "support_materials": ["laid paper"],
        "department": "Prints",
        "collection": "Prints",
        "type": "Print",
        "url": "https://www.clevelandart.org/art/1998.78.14",
        "images": {"web": {"url": "https://openaccess-cdn.clevelandart.org/test_web.jpg"}},
    }
    normalized = normalize_artwork(fake)
    assert normalized is not None
    assert normalized["is_public_domain"] is True
    assert normalized["image_url"].endswith("test_web.jpg")
    assert "Example Artist" in normalized["artist_title"]
    assert metadata_score(normalized) > 100

    copyrighted = dict(fake)
    copyrighted["share_license_status"] = "Copyrighted"
    assert normalize_artwork(copyrighted) is None

    # Synthetic line + tone image test; no network required.
    img = Image.new("L", (W, H), 255)
    for x in range(40, 760):
        y = 80 + int(70 * math.sin(x / 45.0))
        for dy in range(-2, 3):
            if 0 <= y + dy < H:
                img.putpixel((x, y + dy), 0)
    tone = Image.linear_gradient("L").resize((240, 240))
    img.paste(tone, (280, 180))

    for mode in ("line", "atkinson"):
        bw = line_art(img) if mode == "line" else atkinson(img)
        packed = pack_1bpp(bw)
        assert len(packed) == FRAME_BYTES
        assert packed != bytes([0x00]) * FRAME_BYTES
        assert packed != bytes([0xFF]) * FRAME_BYTES

    # Explicit packing/polarity check: first pixel black, next seven white.
    test = Image.new("1", (W, H), 1)
    test.putpixel((0, 0), 0)
    packed = pack_1bpp(test)
    assert packed[0] == 0x7F, f"unexpected first packed byte: 0x{packed[0]:02X}"

    print("Paper Gallery generator self-test passed")


def main() -> None:
    parser = argparse.ArgumentParser(description="Build the Paper Gallery daily e-paper art pack")
    parser.add_argument("--output", default="site/feed", help="Output feed directory")
    parser.add_argument("--site", default="site", help="GitHub Pages site root")
    parser.add_argument("--count", type=int, default=8, help="Number of artworks in the daily pack (1-12)")
    parser.add_argument("--date", help="Override YYYY-MM-DD pack date for testing")
    parser.add_argument("--self-test", action="store_true", help="Run offline tests and exit")
    args = parser.parse_args()

    if args.self_test:
        self_test()
        return
    if not (1 <= args.count <= 12):
        parser.error("--count must be 1..12")

    generate(Path(args.output), Path(args.site), args.count, args.date)


if __name__ == "__main__":
    main()
