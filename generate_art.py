#!/usr/bin/env python3
"""Paper Gallery daily feed generator.

Fetches CC0 Open Access works from the Cleveland Museum of Art, selects
line-art-friendly works, converts them to 800x480 1-bit e-paper frames, and
publishes a small static daily pack.
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
from typing import Iterable

import requests
from PIL import Image, ImageEnhance, ImageFilter, ImageOps, ImageStat

API = "https://openaccess-api.clevelandart.org/api/artworks/"
W, H = 800, 480
FRAME_BYTES = W * H // 8


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
    raw: dict
    score: float
    query: str
    image: Image.Image | None = None
    visual_score: float = 0.0
    mode: str = "atkinson"

    @property
    def id(self) -> str:
        return f"cma-{self.raw.get('accession_number') or self.raw['id']}"


def clean_text(value: object, fallback: str = "") -> str:
    if value is None:
        return fallback
    if isinstance(value, list):
        value = ", ".join(str(x) for x in value if x)
    s = " ".join(str(value).replace("\r", " ").replace("\n", " ").split())
    return s.strip()


def metadata_blob(a: dict) -> str:
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


def metadata_score(a: dict) -> float:
    if not a.get("is_public_domain") or not a.get("image_id"):
        return -9999
    blob = metadata_blob(a)
    score = 0.0
    for term, pts in POSITIVE.items():
        if term in blob:
            score += pts
    for term, pts in NEGATIVE.items():
        if term in blob:
            score += pts

    # Mild preference for drawings/prints that are likely to survive monochrome.
    if "paper" in blob:
        score += 18
    if "black" in blob or "brown ink" in blob:
        score += 12
    return score


def api_search(session: requests.Session, query: str, limit: int = 30) -> tuple[list[dict], str]:
    # Cleveland's API exposes CC0 works and direct web-resolution image URLs.
    params = {
        "q": query,
        "cc0": "",
        "has_image": 1,
        "limit": limit,
    }
    r = session.get(API, params=params, timeout=25)
    r.raise_for_status()
    payload = r.json()

    # Normalize CMA records to the small internal schema used by the rest of
    # the generator. Keeping the downstream image-processing code museum-agnostic
    # makes it easy to add The Met or another source later.
    normalized: list[dict] = []
    for a in payload.get("data", []):
        creators = a.get("creators") or []
        artist = ""
        if creators:
            first = creators[0] or {}
            artist = clean_text(first.get("description") or first.get("name"))
        images = a.get("images") or {}
        web = images.get("web") or {}
        image_url = web.get("url")
        culture = a.get("culture") or []
        support = a.get("support_materials") or []
        normalized.append(
            {
                "id": a.get("id"),
                "accession_number": a.get("accession_number"),
                "title": a.get("title"),
                "artist_title": artist,
                "artist_display": artist,
                "date_display": a.get("creation_date"),
                "image_id": image_url,
                "artwork_type_title": a.get("type"),
                "classification_title": a.get("type"),
                "medium_display": a.get("technique"),
                "technique_titles": a.get("technique"),
                "material_titles": support,
                "subject_titles": [a.get("department"), a.get("collection"), *culture],
                "is_public_domain": str(a.get("share_license_status", "")).upper() == "CC0",
                "source_url": a.get("url"),
            }
        )
    return normalized, "cma-direct"


def gather_candidates(session: requests.Session, seed: int) -> tuple[list[Candidate], str]:
    rng = random.Random(seed)
    terms = SEARCH_TERMS[:]
    rng.shuffle(terms)
    seen: set[int] = set()
    out: list[Candidate] = []
    iiif = DEFAULT_IIIF

    # Eight search requests keeps daily API use modest while still producing variety.
    for q in terms[:8]:
        try:
            rows, iiif = api_search(session, q, 28)
        except requests.RequestException as exc:
            print(f"search failed for {q!r}: {exc}")
            continue
        for a in rows:
            try:
                aid = int(a.get("id"))
            except Exception:
                continue
            if aid in seen:
                continue
            seen.add(aid)
            s = metadata_score(a)
            if s > 15:
                # Small deterministic jitter stops the same famous works dominating.
                s += rng.uniform(-18, 18)
                out.append(Candidate(a, s, q))
    out.sort(key=lambda c: c.score, reverse=True)
    return out, iiif


def download_image(session: requests.Session, c: Candidate, iiif: str) -> Image.Image:
    # CMA provides a direct ~900 px web JPEG for CC0 works, which is ideal for
    # an 800 px e-paper display and avoids downloading multi-megabyte originals.
    url = c.raw.get("image_id")
    if not url:
        raise RuntimeError("No CMA web image URL")
    r = session.get(url, timeout=35)
    r.raise_for_status()
    im = Image.open(io.BytesIO(r.content))
    im.load()
    return im.convert("RGB")


def crop_scan_border(gray: Image.Image) -> Image.Image:
    # Estimate a light paper/background tone from corners and trim only near-white border.
    im = gray.convert("L")
    w, h = im.size
    if w < 20 or h < 20:
        return im
    corners = [
        im.crop((0, 0, max(4, w // 20), max(4, h // 20))),
        im.crop((w - max(4, w // 20), 0, w, max(4, h // 20))),
        im.crop((0, h - max(4, h // 20), max(4, w // 20), h)),
        im.crop((w - max(4, w // 20), h - max(4, h // 20), w, h)),
    ]
    corner_mean = statistics.mean(ImageStat.Stat(x).mean[0] for x in corners)
    if corner_mean < 185:
        return im
    threshold = max(215, min(248, int(corner_mean - 9)))
    mask = im.point(lambda p: 255 if p < threshold else 0)
    bbox = mask.getbbox()
    if not bbox:
        return im
    l, t, r, b = bbox
    pad_x = max(3, int((r - l) * 0.025))
    pad_y = max(3, int((b - t) * 0.025))
    box = (max(0, l - pad_x), max(0, t - pad_y), min(w, r + pad_x), min(h, b + pad_y))
    cropped = im.crop(box)
    # Reject implausibly aggressive trims.
    if cropped.width * cropped.height < 0.28 * w * h:
        return im
    return cropped


def contain_on_canvas(gray: Image.Image) -> Image.Image:
    # Preserve the complete artwork; use white e-paper margin rather than aggressive cropping.
    im = gray.copy()
    max_w, max_h = W - 24, H - 24
    scale = min(max_w / im.width, max_h / im.height)
    new = (max(1, round(im.width * scale)), max(1, round(im.height * scale)))
    im = im.resize(new, Image.Resampling.LANCZOS)
    canvas = Image.new("L", (W, H), 255)
    x = (W - im.width) // 2
    y = (H - im.height) // 2
    canvas.paste(im, (x, y))
    return canvas


def image_metrics(gray: Image.Image) -> dict[str, float]:
    small = gray.resize((200, 120), Image.Resampling.BILINEAR)
    stat = ImageStat.Stat(small)
    mean = stat.mean[0]
    std = stat.stddev[0]
    hist = small.histogram()
    n = small.width * small.height
    white = sum(hist[235:]) / n
    dark = sum(hist[:80]) / n
    mid = sum(hist[80:210]) / n
    edges = small.filter(ImageFilter.FIND_EDGES)
    est = ImageStat.Stat(edges)
    edge_mean = est.mean[0]
    return {"mean": mean, "std": std, "white": white, "dark": dark, "mid": mid, "edge": edge_mean}


def visual_score(metrics: dict[str, float]) -> float:
    # Best e-paper works usually have a fairly light substrate, visible edge structure,
    # and not too much solid black coverage.
    s = 0.0
    s += min(metrics["white"], 0.75) * 80
    s += min(metrics["edge"] / 30.0, 1.5) * 45
    s += min(metrics["std"] / 65.0, 1.5) * 35
    if metrics["dark"] > 0.34:
        s -= (metrics["dark"] - 0.34) * 220
    if metrics["mean"] < 115:
        s -= (115 - metrics["mean"]) * 1.2
    if metrics["white"] < 0.12:
        s -= 35
    return s


def choose_mode(metrics: dict[str, float], blob: str) -> str:
    line_keywords = ("architectural", "botanical", "pen and ink", "graphite", "drawing", "woodcut")
    if any(k in blob for k in line_keywords) and metrics["white"] > 0.28:
        return "line"
    if metrics["mid"] < 0.16 and metrics["white"] > 0.38:
        return "line"
    return "atkinson"


def auto_levels(gray: Image.Image) -> Image.Image:
    # Conservative autocontrast avoids erasing pale hatch marks.
    return ImageOps.autocontrast(gray, cutoff=(0.7, 0.7))


def line_art(gray: Image.Image) -> Image.Image:
    g = auto_levels(gray)
    g = ImageEnhance.Contrast(g).enhance(1.18)
    g = g.filter(ImageFilter.UnsharpMask(radius=0.8, percent=110, threshold=3))
    # Slightly adaptive threshold using a blurred local background estimate.
    local = g.filter(ImageFilter.GaussianBlur(radius=5.0))
    a = list(g.getdata())
    b = list(local.getdata())
    out = bytearray(len(a))
    for i, (p, bg) in enumerate(zip(a, b)):
        threshold = max(128, min(224, bg - 18))
        out[i] = 255 if p >= threshold else 0
    return Image.frombytes("L", g.size, bytes(out)).convert("1", dither=Image.Dither.NONE)


def atkinson(gray: Image.Image) -> Image.Image:
    g = auto_levels(gray)
    g = ImageEnhance.Contrast(g).enhance(1.08)
    w, h = g.size
    px = [float(v) for v in g.getdata()]
    # Atkinson: distribute 1/8 of error to six neighbours.
    for y in range(h):
        row = y * w
        for x in range(w):
            i = row + x
            old = px[i]
            new = 255.0 if old >= 128.0 else 0.0
            px[i] = new
            e = (old - new) / 8.0
            for dx, dy in ((1, 0), (2, 0), (-1, 1), (0, 1), (1, 1), (0, 2)):
                nx, ny = x + dx, y + dy
                if 0 <= nx < w and 0 <= ny < h:
                    j = ny * w + nx
                    px[j] = min(255.0, max(0.0, px[j] + e))
    data = bytes(255 if v >= 128 else 0 for v in px)
    return Image.frombytes("L", (w, h), data).convert("1", dither=Image.Dither.NONE)


def pack_1bpp(img1: Image.Image) -> bytes:
    # Seeed_GFX/TFT_eSprite 1bpp memory is row-major, MSB-first.
    # 1 = white/background, 0 = black/ink for this Paper Gallery feed.
    im = img1.convert("1")
    pixels = im.load()
    out = bytearray(FRAME_BYTES)
    k = 0
    for y in range(H):
        for x0 in range(0, W, 8):
            v = 0
            for bit in range(8):
                if pixels[x0 + bit, y] != 0:
                    v |= 1 << (7 - bit)
            out[k] = v
            k += 1
    return bytes(out)


def evaluate_candidates(session: requests.Session, candidates: list[Candidate], iiif: str, count: int) -> list[Candidate]:
    # Download only the strongest metadata candidates, one at a time.
    shortlist = candidates[: max(count * 3, 18)]
    evaluated: list[Candidate] = []
    for i, c in enumerate(shortlist):
        try:
            c.image = download_image(session, c, iiif)
            gray = ImageOps.grayscale(c.image)
            gray = crop_scan_border(gray)
            canvas = contain_on_canvas(gray)
            metrics = image_metrics(canvas)
            c.visual_score = visual_score(metrics)
            c.mode = choose_mode(metrics, metadata_blob(c.raw))
            c.score += c.visual_score
            c.image = canvas.convert("RGB")
            evaluated.append(c)
            print(f"{i+1:02d}/{len(shortlist)} score={c.score:6.1f} mode={c.mode:8s} {clean_text(c.raw.get('title'))}")
        except Exception as exc:
            print(f"image failed for {c.id}: {exc}")
        time.sleep(1.05)
    evaluated.sort(key=lambda c: c.score, reverse=True)

    # Diversify artists where possible.
    selected: list[Candidate] = []
    artists: set[str] = set()
    for c in evaluated:
        artist = clean_text(c.raw.get("artist_title") or c.raw.get("artist_display"), "Unknown artist")
        key = artist.lower()
        if key in artists and len(selected) < max(3, count // 2):
            continue
        selected.append(c)
        artists.add(key)
        if len(selected) == count:
            break
    if len(selected) < count:
        for c in evaluated:
            if c not in selected:
                selected.append(c)
                if len(selected) == count:
                    break
    return selected


def render_candidate(c: Candidate) -> tuple[Image.Image, bytes]:
    assert c.image is not None
    gray = ImageOps.grayscale(c.image)
    bw = line_art(gray) if c.mode == "line" else atkinson(gray)
    packed = pack_1bpp(bw)
    if len(packed) != FRAME_BYTES:
        raise RuntimeError(f"packed frame is {len(packed)} bytes, expected {FRAME_BYTES}")
    return bw, packed


def source_url(a: dict) -> str:
    return clean_text(a.get("source_url")) or f"https://www.clevelandart.org/art/{a.get('accession_number', a['id'])}"


def write_slot(out: Path, pack: str, slot: int, count: int, c: Candidate) -> dict:
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
        "museum=Cleveland Museum of Art",
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
        "museum": "Cleveland Museum of Art",
        "source": src,
        "mode": c.mode,
        "crc32": f"{crc:08X}",
    }


def write_gallery_html(site_dir: Path, pack: str, works: list[dict]) -> None:
    cards = []
    for w in works:
        cards.append(
            f"""<article><img src="feed/slot{w['slot']}.png" alt="{html.escape(w['title'])}">
            <h2>{html.escape(w['title'])}</h2><p>{html.escape(w['artist'])}</p>
            <p class="muted">{html.escape(w['date'])} · {html.escape(w['mode'])}</p>
            <a href="{html.escape(w['source'])}">Museum record</a></article>"""
        )
    page = f"""<!doctype html><html><head><meta name="viewport" content="width=device-width,initial-scale=1">
    <title>Paper Gallery feed</title><style>
    body{{font-family:system-ui;background:#f3f0e8;color:#171717;margin:0}}main{{max-width:1100px;margin:auto;padding:30px 18px 60px}}
    h1{{font-family:Georgia,serif;font-size:48px;font-weight:500}}.grid{{display:grid;grid-template-columns:repeat(auto-fit,minmax(280px,1fr));gap:22px}}
    article{{background:white;border:1px solid #d4cec1;padding:14px}}img{{width:100%;height:auto;background:white;border:1px solid #eee}}
    h2{{font-family:Georgia,serif;font-weight:500}}.muted{{color:#716c63}}a{{color:#171717}}
    </style></head><body><main><p>PAPER GALLERY</p><h1>Daily public-domain art pack</h1><p>Pack {html.escape(pack)} · Cleveland Museum of Art CC0 Open Access works.</p>
    <div class="grid">{''.join(cards)}</div></main></body></html>"""
    (site_dir / "index.html").write_text(page, encoding="utf-8")


def generate(output: Path, site_dir: Path, count: int, date_str: str | None = None) -> None:
    pack = date_str or dt.date.today().isoformat()
    seed = int(pack.replace("-", ""))
    rng = random.Random(seed)
    session = requests.Session()
    session.headers.update({
        "User-Agent": "PaperGallery/1.1 personal e-paper art frame",
        "Accept": "application/json,image/avif,image/webp,image/apng,image/*,*/*;q=0.8",
    })

    candidates, iiif = gather_candidates(session, seed)
    if len(candidates) < count:
        raise RuntimeError(f"Only {len(candidates)} viable metadata candidates")

    # Rotate the top pool deterministically by day before visual evaluation.
    top = candidates[: min(70, len(candidates))]
    rng.shuffle(top)
    top.sort(key=lambda c: c.score, reverse=True)
    selected = evaluate_candidates(session, top, iiif, count)
    if len(selected) < count:
        raise RuntimeError(f"Only {len(selected)} downloadable candidates, need {count}")

    output.mkdir(parents=True, exist_ok=True)
    site_dir.mkdir(parents=True, exist_ok=True)
    works = [write_slot(output, pack, i, count, c) for i, c in enumerate(selected)]
    (output / "index.txt").write_text(f"PG1\npack={pack}\ncount={count}\n", encoding="utf-8")
    (output / "index.json").write_text(json.dumps({"pack": pack, "count": count, "works": works}, ensure_ascii=False, indent=2), encoding="utf-8")
    write_gallery_html(site_dir, pack, works)
    print(f"Wrote {count} artworks to {output}")


def self_test() -> None:
    # Synthetic line and tone image test; no network needed.
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
    print("Paper Gallery generator self-test passed")


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--output", default="site/feed")
    p.add_argument("--site", default="site")
    p.add_argument("--count", type=int, default=8)
    p.add_argument("--date", help="Override YYYY-MM-DD pack date for testing")
    p.add_argument("--self-test", action="store_true")
    args = p.parse_args()
    if args.self_test:
        self_test()
        return
    if not (1 <= args.count <= 12):
        p.error("--count must be 1..12")
    generate(Path(args.output), Path(args.site), args.count, args.date)


if __name__ == "__main__":
    main()
