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
    "architectural drawing",
    "botanical illustration",
    "natural history illustration",
    "scientific illustration",
    "etching",
    "engraving",
    "woodcut",
    "wood engraving",
    "drypoint",
    "pen and ink drawing",
    "ink drawing",
    "graphite drawing",
    "line engraving landscape",
    "city view engraving",
    "architectural print",
    "landscape drawing",
    "lithograph",
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
    "poster": -75,
    "manuscript": -75,
    "nude": -18,
    "mythological": -12,
}


@dataclass
class Candidate:
    raw: dict[str, Any]
    score: float
    query: str
    image: Image.Image | None = None
    visual_score: float = 0.0
    mode: str = "atkinson"
    content_box: tuple[int, int, int, int] | None = None
    preview_bw: Image.Image | None = None
    preview_ink: float = 0.0
    preview_content_ink: float = 0.0
    aspect_ratio: float = 1.0
    orientation: str = "square"
    active_fraction: float = 0.0

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
            "User-Agent": "PaperGallery/1.3 (+personal e-paper art frame)",
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
    for q in terms[:14]:
        try:
            rows = api_search(session, q, 48)
        except (requests.RequestException, ValueError, RuntimeError) as exc:
            print(f"search failed for {q!r}: {exc}")
            continue

        for a in rows:
            object_key = clean_text(a.get("accession_number")) or clean_text(a.get("id"))
            if not object_key or object_key in seen:
                continue
            seen.add(object_key)
            score = metadata_score(a)
            if score > 22:
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


def contain_on_canvas_with_box(gray: Image.Image) -> tuple[Image.Image, tuple[int, int, int, int]]:
    """Fit the complete artwork on an 800x480 white canvas and return its fitted box."""
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
    return canvas, (x, y, x + im.width, y + im.height)


def contain_on_canvas(gray: Image.Image) -> Image.Image:
    canvas, _ = contain_on_canvas_with_box(gray)
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


def active_area_fraction(gray: Image.Image) -> float:
    """How much of the artwork rectangle is actually occupied by visible marks."""
    small = gray.convert("L").resize((240, 240), Image.Resampling.BILINEAR)
    mask = small.point(lambda p: 255 if p < 238 else 0)
    bbox = mask.getbbox()
    if not bbox:
        return 0.0
    l, t, r, b = bbox
    return ((r - l) * (b - t)) / float(small.width * small.height)


def visual_score(metrics: dict[str, float]) -> float:
    """Score how naturally an artwork should survive 1-bit e-paper conversion.

    Paper Gallery deliberately prefers crisp drawings with visible paper and
    moderate structure. Very dark, muddy, or very flat/low-contrast images are
    penalized.
    """
    score = 0.0
    score += min(metrics["white"], 0.88) * 110
    score += min(metrics["edge"] / 28.0, 1.6) * 52
    score += min(metrics["std"] / 58.0, 1.5) * 40

    if metrics["dark"] > 0.17:
        score -= (metrics["dark"] - 0.17) * 420
    if metrics["mean"] < 168:
        score -= (168 - metrics["mean"]) * 1.8
    if metrics["mid"] > 0.50:
        score -= (metrics["mid"] - 0.50) * 140
    if metrics["white"] < 0.22:
        score -= 60
    if metrics["std"] < 24:
        score -= (24 - metrics["std"]) * 1.6
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
        "wood engraving",
        "illustration",
        "scientific",
    )
    tonal_keywords = ("mezzotint", "aquatint", "tonal", "wash", "charcoal")
    if any(k in blob for k in line_keywords):
        return "line"
    if any(k in blob for k in tonal_keywords) and metrics["mid"] > 0.28:
        return "atkinson"
    if metrics["white"] > 0.26 and metrics["mid"] < 0.34:
        return "line"
    if metrics["std"] < 24 and metrics["mid"] > 0.28:
        return "atkinson"
    if metrics["dark"] < 0.12 and metrics["edge"] > 11:
        return "line"
    return "line"


def auto_levels(gray: Image.Image) -> Image.Image:
    # Gentle normalization. Heavy autocontrast makes aged paper and pale wash
    # turn into dark texture on a 1-bit screen.
    return ImageOps.autocontrast(gray.convert("L"), cutoff=(0.25, 0.25))


def lift_midtones(gray: Image.Image, gamma: float = 0.76, offset: int = 6) -> Image.Image:
    """Brighten paper/midtones while leaving genuinely dark ink recognizable."""
    if gamma <= 0:
        raise ValueError("gamma must be positive")
    lut = []
    for i in range(256):
        v = 255.0 * ((i / 255.0) ** gamma) + offset
        lut.append(max(0, min(255, round(v))))
    return gray.convert("L").point(lut)


def boost_contrast(gray: Image.Image, contrast: float = 1.12, sharpen: float = 1.06) -> Image.Image:
    g = ImageEnhance.Contrast(gray.convert("L")).enhance(contrast)
    g = g.filter(ImageFilter.UnsharpMask(radius=0.8, percent=int((sharpen - 1.0) * 100 + 95), threshold=3))
    return g


def line_art(
    gray: Image.Image,
    *,
    gamma: float = 0.72,
    offset: int = 8,
    local_gap: int = 48,
    contrast: float = 1.12,
) -> Image.Image:
    """Render clean drawings with intentionally restrained black coverage."""
    g = boost_contrast(lift_midtones(auto_levels(gray), gamma=gamma, offset=offset), contrast=contrast)

    local = g.filter(ImageFilter.GaussianBlur(radius=5.0))
    a = g.tobytes()
    b = local.tobytes()
    out = bytearray(len(a))
    for i, (p, bg) in enumerate(zip(a, b)):
        threshold = max(112, min(204, bg - local_gap))
        out[i] = 255 if p >= threshold else 0
    return Image.frombytes("L", g.size, bytes(out)).convert("1", dither=Image.Dither.NONE)


def atkinson(
    gray: Image.Image,
    *,
    gamma: float = 0.78,
    offset: int = 6,
    threshold: float = 116.0,
    contrast: float = 1.08,
) -> Image.Image:
    """Light-biased Atkinson dithering for engravings and tonal prints."""
    g = boost_contrast(lift_midtones(auto_levels(gray), gamma=gamma, offset=offset), contrast=contrast)
    w, h = g.size
    px = [float(v) for v in g.tobytes()]

    for y in range(h):
        row = y * w
        for x in range(w):
            i = row + x
            old = px[i]
            new = 255.0 if old >= threshold else 0.0
            px[i] = new
            error = (old - new) / 8.0
            for dx, dy in ((1, 0), (2, 0), (-1, 1), (0, 1), (1, 1), (0, 2)):
                nx, ny = x + dx, y + dy
                if 0 <= nx < w and 0 <= ny < h:
                    j = ny * w + nx
                    px[j] = min(255.0, max(0.0, px[j] + error))

    data = bytes(255 if v >= threshold else 0 for v in px)
    return Image.frombytes("L", (w, h), data).convert("1", dither=Image.Dither.NONE)


def black_fraction(
    gray: Image.Image,
    *,
    gamma: float = 0.78,
    offset: int = 6,
    threshold: float = 116.0,
) -> Image.Image:
    """Light-biased Atkinson dithering for engravings and tonal prints."""
    g = lift_midtones(auto_levels(gray), gamma=gamma, offset=offset)
    w, h = g.size
    px = [float(v) for v in g.tobytes()]

    # A threshold below 128 intentionally biases the result toward white. On
    # monochrome e-paper this preserves the look of paper instead of allowing
    # gray engraving tone to collapse into large black regions.
    for y in range(h):
        row = y * w
        for x in range(w):
            i = row + x
            old = px[i]
            new = 255.0 if old >= threshold else 0.0
            px[i] = new
            error = (old - new) / 8.0
            for dx, dy in ((1, 0), (2, 0), (-1, 1), (0, 1), (1, 1), (0, 2)):
                nx, ny = x + dx, y + dy
                if 0 <= nx < w and 0 <= ny < h:
                    j = ny * w + nx
                    px[j] = min(255.0, max(0.0, px[j] + error))

    data = bytes(255 if v >= threshold else 0 for v in px)
    return Image.frombytes("L", (w, h), data).convert("1", dither=Image.Dither.NONE)


def black_fraction(img1: Image.Image) -> float:
    """Fraction of the final frame that is black ink (0.0 to 1.0)."""
    hist = img1.convert("1").histogram()
    black = hist[0] if hist else 0
    total = img1.width * img1.height
    return black / total if total else 0.0


def black_fraction_in_box(img1: Image.Image, box: tuple[int, int, int, int] | None) -> float:
    """Black coverage inside the fitted artwork area, excluding white frame margins."""
    if box is None:
        return black_fraction(img1)
    l, t, r, b = box
    l, t = max(0, l), max(0, t)
    r, b = min(img1.width, r), min(img1.height, b)
    if r <= l or b <= t:
        return black_fraction(img1)
    return black_fraction(img1.crop((l, t, r, b)))

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


def classify_orientation(ratio: float) -> str:
    if ratio >= 1.15:
        return "horizontal"
    if ratio >= 0.90:
        return "square"
    return "vertical"


def aspect_score(ratio: float) -> float:
    """Landscape-biased score tuned for an 800x480 horizontal frame."""
    if ratio <= 0:
        return -120.0
    if 1.35 <= ratio <= 2.25:
        return 52.0
    if 1.15 <= ratio < 1.35:
        return 34.0
    if 2.25 < ratio <= 2.75:
        return 28.0
    if 0.90 <= ratio < 1.15:
        return 6.0
    if 0.72 <= ratio < 0.90:
        return -38.0
    if ratio < 0.72:
        return -92.0
    return -12.0


def evaluate_candidates(session: requests.Session, candidates: list[Candidate], count: int) -> list[Candidate]:
    """Download, render and rank candidates for a light, paper-like e-paper result."""
    max_to_try = min(len(candidates), max(count * 18, 96))
    evaluated: list[Candidate] = []

    for i, c in enumerate(candidates[:max_to_try]):
        try:
            image = download_image(session, c)
            cropped = crop_scan_border(ImageOps.grayscale(image))
            metrics = image_metrics(cropped)
            active_fraction = active_area_fraction(cropped)
            ratio = cropped.width / cropped.height if cropped.height else 1.0
            orientation = classify_orientation(ratio)

            title = clean_text(c.raw.get("title"), "Untitled")

            if metrics["mean"] < 132 or metrics["dark"] > 0.34:
                print(f"{i + 1:02d}/{max_to_try} reject source-dark  {title}")
                continue
            if metrics["white"] < 0.10 and metrics["mean"] < 158:
                print(f"{i + 1:02d}/{max_to_try} reject low-paper    {title}")
                continue
            if metrics["std"] < 18:
                print(f"{i + 1:02d}/{max_to_try} reject low-contrast {title}")
                continue
            if active_fraction < 0.18:
                print(f"{i + 1:02d}/{max_to_try} reject tiny-content {title}")
                continue
            if ratio < 0.62 and visual_score(metrics) < 95:
                print(f"{i + 1:02d}/{max_to_try} reject narrow-portrait ratio={ratio:.2f} {title}")
                continue

            canvas, box = contain_on_canvas_with_box(cropped)
            c.content_box = box
            c.visual_score = visual_score(metrics)
            c.mode = choose_mode(metrics, metadata_blob(c.raw))
            c.aspect_ratio = ratio
            c.orientation = orientation
            c.active_fraction = active_fraction
            c.score += c.visual_score + aspect_score(ratio)
            c.image = canvas.convert("RGB")

            bw, _, ink = render_candidate(c)
            content_ink = c.preview_content_ink

            hard_global = 0.125 if c.mode == "line" else 0.145
            hard_content = 0.23 if c.mode == "line" else 0.25
            min_global = 0.055 if c.mode == "line" else 0.060
            min_content = 0.125 if c.mode == "line" else 0.135

            if ink > hard_global or content_ink > hard_content:
                print(
                    f"{i + 1:02d}/{max_to_try} reject dense       "
                    f"global={ink*100:4.1f}% content={content_ink*100:4.1f}% {title}"
                )
                continue
            if ink < min_global or content_ink < min_content:
                print(
                    f"{i + 1:02d}/{max_to_try} reject faint       "
                    f"global={ink*100:4.1f}% content={content_ink*100:4.1f}% {title}"
                )
                continue

            ideal_global = 0.078 if c.mode == "line" else 0.090
            ideal_content = 0.158 if c.mode == "line" else 0.170
            c.score -= abs(ink - ideal_global) * 340
            c.score -= abs(content_ink - ideal_content) * 240
            c.score += min(active_fraction, 0.65) * 18
            if active_fraction < 0.28:
                c.score -= 18

            evaluated.append(c)
            print(
                f"{i + 1:02d}/{max_to_try} score={c.score:6.1f} mode={c.mode:8s} "
                f"ink={ink*100:4.1f}% content={content_ink*100:4.1f}% "
                f"active={active_fraction*100:4.1f}% ratio={ratio:.2f} {orientation:10s} {title}"
            )
        except Exception as exc:
            print(f"image failed for {c.id}: {exc}")

        time.sleep(0.20)

        if len(evaluated) >= max(count * 4, count + 16) and i + 1 >= max(count * 6, 48):
            break

    evaluated.sort(key=lambda c: c.score, reverse=True)

    # Compose a pack for the physical horizontal frame, not merely the gallery page.
    # For an 8-work pack we target 6 landscape works (75%), allow square-ish works
    # for variety, and cap true portrait works at one.
    target_horizontal = max(1, math.ceil(count * 0.75))
    min_horizontal = max(1, math.ceil(count * 0.625))
    max_vertical = 1 if count >= 4 else 0

    selected: list[Candidate] = []
    artists: set[str] = set()

    def artist_key(c: Candidate) -> str:
        return clean_text(c.raw.get("artist_title") or c.raw.get("artist_display"), "Unknown artist").casefold()

    def add_candidate(c: Candidate, enforce_artist_diversity: bool = True) -> bool:
        if c in selected:
            return False
        key = artist_key(c)
        if enforce_artist_diversity and key in artists:
            return False
        selected.append(c)
        artists.add(key)
        return True

    horizontals = [c for c in evaluated if c.orientation == "horizontal"]
    squares = [c for c in evaluated if c.orientation == "square"]
    verticals = [c for c in evaluated if c.orientation == "vertical"]

    # First reserve the majority of the pack for works that naturally fit 800x480.
    for c in horizontals:
        if len([x for x in selected if x.orientation == "horizontal"]) >= target_horizontal:
            break
        add_candidate(c, enforce_artist_diversity=True)

    # If artist diversity prevented the landscape target, relax only that constraint.
    for c in horizontals:
        if len([x for x in selected if x.orientation == "horizontal"]) >= target_horizontal:
            break
        add_candidate(c, enforce_artist_diversity=False)

    # Fill remaining slots primarily with square-ish works.
    for c in squares:
        if len(selected) >= count:
            break
        add_candidate(c, enforce_artist_diversity=True)
    for c in squares:
        if len(selected) >= count:
            break
        add_candidate(c, enforce_artist_diversity=False)

    # Permit at most one portrait as a special piece, and only after horizontal/square.
    vertical_added = 0
    for c in verticals:
        if len(selected) >= count or vertical_added >= max_vertical:
            break
        if add_candidate(c, enforce_artist_diversity=True):
            vertical_added += 1
    for c in verticals:
        if len(selected) >= count or vertical_added >= max_vertical:
            break
        if add_candidate(c, enforce_artist_diversity=False):
            vertical_added += 1

    # If strict composition still left slots empty, use the best remaining works,
    # but keep at least the minimum horizontal share whenever the pool supports it.
    for c in evaluated:
        if len(selected) >= count:
            break
        current_h = sum(x.orientation == "horizontal" for x in selected)
        remaining_slots = count - len(selected)
        if current_h < min_horizontal and remaining_slots <= (min_horizontal - current_h):
            if c.orientation != "horizontal":
                continue
        if c.orientation == "vertical" and sum(x.orientation == "vertical" for x in selected) >= max_vertical:
            continue
        add_candidate(c, enforce_artist_diversity=False)

    selected.sort(key=lambda c: c.score, reverse=True)
    return selected[:count]


def render_candidate(c: Candidate) -> tuple[Image.Image, bytes, float]:
    """Render with density control measured both globally and within the artwork."""
    if c.image is None:
        raise RuntimeError(f"candidate {c.id} has no prepared image")
    gray = ImageOps.grayscale(c.image)

    if c.mode == "line":
        global_min, global_max, global_center = 0.060, 0.095, 0.078
        content_min, content_max, content_center = 0.135, 0.180, 0.158
        profiles = [
            (0.82, 4, 40, 1.16),
            (0.76, 6, 46, 1.14),
            (0.70, 10, 52, 1.12),
            (0.64, 14, 58, 1.10),
            (0.58, 18, 64, 1.08),
        ]
        rendered = [line_art(gray, gamma=g, offset=o, local_gap=gap, contrast=ct) for g, o, gap, ct in profiles]
    else:
        global_min, global_max, global_center = 0.065, 0.115, 0.090
        content_min, content_max, content_center = 0.140, 0.195, 0.170
        profiles = [
            (0.84, 4, 122.0, 1.10),
            (0.80, 6, 118.0, 1.09),
            (0.76, 8, 114.0, 1.08),
            (0.70, 12, 108.0, 1.07),
            (0.64, 16, 102.0, 1.06),
        ]
        rendered = [atkinson(gray, gamma=g, offset=o, threshold=t, contrast=ct) for g, o, t, ct in profiles]

    best = None
    best_score = float('inf')
    for attempt in rendered:
        attempt_ink = black_fraction(attempt)
        attempt_content = black_fraction_in_box(attempt, c.content_box)
        penalty = 0.0
        # Strong penalties outside the acceptable bands.
        if attempt_ink < global_min:
            penalty += (global_min - attempt_ink) * 2600
        if attempt_ink > global_max:
            penalty += (attempt_ink - global_max) * 2600
        if attempt_content < content_min:
            penalty += (content_min - attempt_content) * 2100
        if attempt_content > content_max:
            penalty += (attempt_content - content_max) * 2100
        # Softer preference for the center of the band.
        penalty += abs(attempt_ink - global_center) * 260
        penalty += abs(attempt_content - content_center) * 190
        if penalty < best_score:
            best_score = penalty
            best = (attempt, attempt_ink, attempt_content)

    assert best is not None
    bw, ink, content_ink = best
    c.preview_bw = bw
    c.preview_ink = ink
    c.preview_content_ink = content_ink

    packed = pack_1bpp(bw)
    if len(packed) != FRAME_BYTES:
        raise RuntimeError(f"packed frame is {len(packed)} bytes, expected {FRAME_BYTES}")
    return bw, packed, ink


def source_url(a: dict[str, Any]) -> str:
    src = clean_text(a.get("source_url"))
    if src:
        return src
    accession = clean_text(a.get("accession_number"))
    if accession:
        return f"https://www.clevelandart.org/art/{quote(accession, safe='')}"
    return f"https://www.clevelandart.org/art/{quote(clean_text(a.get('id'), 'unknown'), safe='')}"


def write_slot(out: Path, pack: str, slot: int, count: int, c: Candidate) -> dict[str, Any]:
    bw, packed, ink = render_candidate(c)
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
        f"orientation={c.orientation}",
        f"aspect={c.aspect_ratio:.2f}",
        f"ink={ink * 100:.1f}",
        f"content_ink={c.preview_content_ink * 100:.1f}",
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
        "orientation": c.orientation,
        "aspect": round(c.aspect_ratio, 2),
        "ink": round(ink * 100, 1),
        "content_ink": round(c.preview_content_ink * 100, 1),
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
        orientation = html.escape(str(work.get("orientation", "?")), quote=True)
        aspect = html.escape(str(work.get("aspect", "?")), quote=True)
        ink = html.escape(str(work.get("ink", "?")), quote=True)
        content_ink = html.escape(str(work.get("content_ink", "?")), quote=True)
        source = html.escape(str(work["source"]), quote=True)
        slot = int(work["slot"])
        cards.append(
            f'<article><img src="feed/slot{slot}.png" alt="{title}">'
            f"<h2>{title}</h2><p>{artist}</p>"
            f'<p class="muted">{date} · {orientation} {aspect}:1 · {mode} · {ink}% frame ink · {content_ink}% artwork ink</p>'
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

    # Orientation bias checks for the physical 800x480 landscape frame.
    assert classify_orientation(1.50) == "horizontal"
    assert classify_orientation(1.00) == "square"
    assert classify_orientation(0.75) == "vertical"
    assert aspect_score(1.60) > aspect_score(1.00) > aspect_score(0.75)

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

    # Density control sanity check on a deliberately dark tonal test frame.
    dark = Image.new("L", (W, H), 150)
    for x in range(80, 720, 8):
        for y in range(70, 410):
            if (x + y) % 19 == 0:
                dark.putpixel((x, y), 35)
    dummy = Candidate(normalized, 0, "test", image=dark.convert("RGB"), mode="atkinson", content_box=(0, 0, W, H))
    _, packed_dark, ink = render_candidate(dummy)
    assert len(packed_dark) == FRAME_BYTES
    assert ink < 0.18, f"density control failed: {ink:.3f}"
    assert dummy.preview_content_ink < 0.22, f"content density control failed: {dummy.preview_content_ink:.3f}"

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
