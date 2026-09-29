#!/usr/bin/env python3
"""Paper Gallery Aerospace Drawings feed generator.

Sources: Wikimedia Commons plus rights-checked NASA Technical Reports Server (NTRS).

The generator searches Commons for aerospace technical imagery, verifies each
file is explicitly Public Domain or CC0 using Commons extended image metadata,
converts suitable imagery to a captioned 800x480 1-bit e-paper frame, and
publishes one master Aerospace feed. Each slot carries a category; the ESP32
filters the pack locally when the user chooses a category.

NTRS PDF pages are ranked and rendered automatically; future adapters can add
Library of Congress and Smithsonian Open Access without changing the ESP32 feed format.
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
import re
import statistics
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import requests
import pypdfium2 as pdfium
from PIL import Image, ImageDraw, ImageEnhance, ImageFilter, ImageFont, ImageOps, ImageStat
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

COMMONS_API = "https://commons.wikimedia.org/w/api.php"
NTRS_API = "https://ntrs.nasa.gov/api"
SOURCE_NAME = "Wikimedia Commons"
NTRS_SOURCE_NAME = "NASA Technical Reports Server"
W, H = 800, 480
CAPTION_H = 66
ART_H = H - CAPTION_H
FRAME_BYTES = W * H // 8

# Strict allow-list: user requested public-domain or CC0 imagery only.
LICENSE_ALLOW = {
    "public domain",
    "pd",
    "cc0",
    "cc0 1.0",
    "creative commons cc0 1.0 universal public domain dedication",
}

CATEGORY_ORDER = [
    "aircraft-three-views",
    "x-planes-experimental",
    "spacecraft",
    "rockets-launch-vehicles",
    "aerodynamics",
    "propulsion",
    "wind-tunnel-models",
    "airfoils",
    "historical-aviation",
    "odd-abandoned-concepts",
]

CATEGORY_LABELS = {
    "aircraft-three-views": "Aircraft Three-Views",
    "x-planes-experimental": "X-Planes / Experimental Aircraft",
    "spacecraft": "Spacecraft",
    "rockets-launch-vehicles": "Rockets / Launch Vehicles",
    "aerodynamics": "Aerodynamics",
    "propulsion": "Propulsion",
    "wind-tunnel-models": "Wind-Tunnel Models",
    "airfoils": "Airfoils",
    "historical-aviation": "Historical Aviation",
    "odd-abandoned-concepts": "Odd / Abandoned Concepts",
    "random-aerospace": "Random Aerospace",
}

# Multiple searches per category improves variety. Search is constrained to
# Commons' File namespace and bitmap media; rights are verified afterwards.
SEARCHES = {
    "aircraft-three-views": [
        '"3-view" aircraft NACA filetype:bitmap',
        '"3-view drawing" NACA aircraft filetype:bitmap',
        '"three-view" aircraft drawing NASA filetype:bitmap',
        'orthographic aircraft drawing NACA filetype:bitmap',
    ],
    "x-planes-experimental": [
        'NASA X-plane drawing filetype:bitmap',
        'NACA experimental aircraft drawing filetype:bitmap',
        'NASA lifting body diagram filetype:bitmap',
        'experimental aircraft configuration NASA filetype:bitmap',
        'supersonic research aircraft drawing NASA filetype:bitmap',
    ],
    "spacecraft": [
        'NASA spacecraft configuration diagram filetype:bitmap',
        'Apollo spacecraft diagram NASA filetype:bitmap',
        'Gemini spacecraft diagram NASA filetype:bitmap',
        'Mercury spacecraft diagram NASA filetype:bitmap',
        'spacecraft cutaway NASA filetype:bitmap',
    ],
    "rockets-launch-vehicles": [
        'NASA launch vehicle diagram filetype:bitmap',
        'Saturn V diagram NASA filetype:bitmap',
        'rocket schematic NASA filetype:bitmap',
        'launch vehicle configuration NASA filetype:bitmap',
    ],
    "aerodynamics": [
        'NACA aerodynamic diagram filetype:bitmap',
        'NASA aerodynamic configuration drawing filetype:bitmap',
        'NACA flow diagram aircraft filetype:bitmap',
        'aerodynamic study NACA drawing filetype:bitmap',
    ],
    "propulsion": [
        'NASA engine schematic filetype:bitmap',
        'NACA propulsion diagram filetype:bitmap',
        'turbojet schematic NASA filetype:bitmap',
        'rocket engine schematic NASA filetype:bitmap',
    ],
    "wind-tunnel-models": [
        'NACA wind tunnel model drawing filetype:bitmap',
        'NASA wind-tunnel model diagram filetype:bitmap',
        'wind tunnel model configuration NACA filetype:bitmap',
    ],
    "airfoils": [
        'NACA airfoil diagram filetype:bitmap',
        'NACA airfoil section drawing filetype:bitmap',
        'airfoil profile NACA filetype:bitmap',
        'wing section NACA diagram filetype:bitmap',
    ],
    "historical-aviation": [
        'NACA Aircraft Circular 3-view filetype:bitmap',
        'early aircraft plan public domain filetype:bitmap',
        'airship drawing public domain aviation filetype:bitmap',
        'historic aircraft engineering drawing NACA filetype:bitmap',
    ],
    "odd-abandoned-concepts": [
        'NASA concept aircraft drawing filetype:bitmap',
        'NACA unusual aircraft configuration filetype:bitmap',
        'NASA abandoned aircraft concept filetype:bitmap',
        'NASA lifting body concept drawing filetype:bitmap',
        'NASA supersonic transport concept diagram filetype:bitmap',
    ],
}

NTRS_SEARCHES = {
    "aircraft-three-views": [
        "three-view drawing aircraft",
        "aircraft configuration drawing",
        "general arrangement aircraft",
    ],
    "x-planes-experimental": [
        "research aircraft configuration",
        "experimental aircraft configuration",
        "lifting body configuration",
        "supersonic research aircraft",
    ],
    "spacecraft": [
        "spacecraft configuration diagram",
        "spacecraft general arrangement",
        "Apollo spacecraft configuration",
        "lifting body spacecraft configuration",
    ],
    "rockets-launch-vehicles": [
        "launch vehicle configuration",
        "rocket configuration diagram",
        "Saturn launch vehicle configuration",
    ],
    "aerodynamics": [
        "aerodynamic configuration figure",
        "aerodynamic study aircraft configuration",
        "flow field diagram aircraft",
    ],
    "propulsion": [
        "engine schematic propulsion",
        "turbojet schematic",
        "rocket engine schematic",
        "propulsion system diagram",
    ],
    "wind-tunnel-models": [
        "wind tunnel model configuration",
        "wind-tunnel model drawing",
        "wind tunnel test model",
    ],
    "airfoils": [
        "airfoil section figure",
        "airfoil profile NACA",
        "wing section airfoil",
    ],
    "historical-aviation": [
        "NACA aircraft configuration",
        "NACA aircraft drawing",
        "NACA aircraft circular",
    ],
    "odd-abandoned-concepts": [
        "advanced aircraft concept configuration",
        "unconventional aircraft configuration",
        "supersonic transport concept",
        "lifting body concept",
    ],
}

NTRS_PAGE_TERMS = {
    "aircraft-three-views": ("three-view", "3-view", "general arrangement", "configuration", "planform", "side view", "front view"),
    "x-planes-experimental": ("configuration", "research aircraft", "experimental", "lifting body", "concept", "general arrangement"),
    "spacecraft": ("spacecraft", "configuration", "arrangement", "capsule", "module", "vehicle"),
    "rockets-launch-vehicles": ("launch vehicle", "rocket", "configuration", "stage", "booster", "vehicle"),
    "aerodynamics": ("aerodynamic", "flow", "pressure", "configuration", "mach", "model"),
    "propulsion": ("engine", "propulsion", "turbine", "compressor", "nozzle", "schematic", "combustor"),
    "wind-tunnel-models": ("wind tunnel", "model", "test configuration", "sting", "tunnel"),
    "airfoils": ("airfoil", "section", "profile", "wing section", "coordinates"),
    "historical-aviation": ("aircraft", "configuration", "NACA", "arrangement", "drawing"),
    "odd-abandoned-concepts": ("concept", "configuration", "advanced", "unconventional", "lifting body", "supersonic"),
}

NTRS_RIGHTS_ALLOW = {
    "GOV_PUBLIC_USE_PERMITTED",
    "PUBLIC_USE_PERMITTED",
}
NTRS_MAX_PDF_BYTES = 30 * 1024 * 1024
NTRS_MAX_PAGES_TO_RENDER = 12
NTRS_MAX_FEATURES_PER_DAY = 7
NTRS_MAX_PDF_ATTEMPTS_PER_DAY = 12
NTRS_MAX_PDF_ATTEMPTS_PER_CATEGORY = 2
NTRS_MAX_PDF_BYTES_PER_DAY = 180 * 1024 * 1024
NTRS_MAX_PAGES_TO_INSPECT = 120
NTRS_MAX_RENDER_PIXELS = 4_500_000

TECHNICAL_TERMS = (
    "drawing", "diagram", "schematic", "three-view", "3-view", "orthographic",
    "configuration", "plan", "section", "cutaway", "profile", "airfoil", "model",
    "naca", "nasa", "technical", "figure", "blueprint", "projection", "view",
)
PHOTO_TERMS = ("photograph", "photo ", "photographic", "portrait", "ceremony", "crew photo")

REPORT_PATTERNS = [
    re.compile(r"\bNACA[- ]?(?:AC|TR|TN|TM|RM|WR|SR)[- ]?[A-Z0-9.()/-]+", re.I),
    re.compile(r"\bNASA[- /]?(?:TN|TM|TP|CR|SP|RP)[- /]?[A-Z0-9.()/-]+", re.I),
    re.compile(r"\bNACA\s+Aircraft\s+Circular\s+(?:No\.?\s*)?\d+[A-Z-]*", re.I),
]


@dataclass
class Candidate:
    pageid: int
    file_title: str
    image_url: str
    description_url: str
    title: str
    description: str
    artist: str
    date_text: str
    categories_text: str
    license_short: str
    copyright_status: str
    restrictions: str
    category: str
    search_term: str
    width: int
    height: int
    source_kind: str = "commons"
    source_name: str = SOURCE_NAME
    source_id: str = ""
    pdf_page: int = 0
    report: str = ""
    year: str = ""
    score: float = 0.0
    frame: Image.Image | None = None
    ink: float = 0.0

    @property
    def id(self) -> str:
        if self.source_kind == "ntrs":
            return f"ntrs-{self.source_id}-p{self.pdf_page}"
        return f"commons-{self.pageid}"


@dataclass
class NTRSRecord:
    record_id: str
    title: str
    abstract: str
    category: str
    report: str
    year: str
    citation_url: str
    pdf_url: str
    rights: str
    subject_text: str
    score: float = 0.0


@dataclass
class NTRSBudget:
    attempts: int = 0
    bytes_downloaded: int = 0

    def can_attempt(self) -> bool:
        return (
            self.attempts < NTRS_MAX_PDF_ATTEMPTS_PER_DAY
            and self.bytes_downloaded < NTRS_MAX_PDF_BYTES_PER_DAY
        )

    def note_attempt(self) -> None:
        self.attempts += 1

    def note_bytes(self, count: int) -> None:
        self.bytes_downloaded += max(0, int(count))


# -------------------- Generic helpers --------------------

def clean_html_text(value: object) -> str:
    if value is None:
        return ""
    if isinstance(value, dict):
        value = " ".join(clean_html_text(v) for v in value.values() if v not in (None, "", [], {}))
    elif isinstance(value, (list, tuple, set)):
        value = " ".join(clean_html_text(v) for v in value if v not in (None, "", [], {}))
    s = str(value)
    s = re.sub(r"<br\s*/?>", " ", s, flags=re.I)
    s = re.sub(r"<[^>]+>", " ", s)
    s = html.unescape(s)
    s = re.sub(r"\s+", " ", s).strip()
    return s


def ext_value(meta: dict[str, Any], key: str) -> str:
    item = meta.get(key)
    if isinstance(item, dict):
        return clean_html_text(item.get("value"))
    return clean_html_text(item)


def make_session() -> requests.Session:
    retry = Retry(
        total=4,
        connect=4,
        read=4,
        status=4,
        backoff_factor=0.7,
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=frozenset({"GET", "POST"}),
        respect_retry_after_header=True,
    )
    adapter = HTTPAdapter(max_retries=retry, pool_connections=6, pool_maxsize=6)
    s = requests.Session()
    s.mount("https://", adapter)
    s.headers.update({
        "User-Agent": "PaperGallery/2.0 (personal e-paper aerospace gallery; Wikimedia Commons API)",
        "Accept": "*/*",
    })
    return s


def license_is_allowed(license_short: str, copyrighted: str, restrictions: str, deletion_reason: str) -> bool:
    lic = clean_html_text(license_short).casefold().strip()
    copy = clean_html_text(copyrighted).casefold().strip()
    rest = clean_html_text(restrictions).strip()
    deletion = clean_html_text(deletion_reason).strip()

    allowed = lic in LICENSE_ALLOW or lic.startswith("public domain") or lic.startswith("cc0")
    # CommonsMetadata documents Copyrighted=False for public-domain files.
    if copy in {"true", "1", "yes"}:
        return False
    if rest:
        return False
    if deletion:
        return False
    return allowed


def extract_report(*parts: str) -> str:
    text = " ".join(p for p in parts if p)
    for pattern in REPORT_PATTERNS:
        m = pattern.search(text)
        if m:
            value = re.sub(r"\s+", " ", m.group(0)).strip(" .,;:")
            return value[:72]
    return ""


def extract_year(date_text: str, *fallback_parts: str) -> str:
    text = " ".join([date_text, *fallback_parts])
    years = re.findall(r"\b(18\d{2}|19\d{2}|20\d{2})\b", text)
    return years[0] if years else ""


def normalized_display_title(file_title: str, object_name: str, description: str) -> str:
    title = clean_html_text(object_name)
    if not title:
        title = re.sub(r"^File:", "", file_title, flags=re.I)
        title = re.sub(r"\.(?:png|jpe?g|tiff?|gif)$", "", title, flags=re.I)
        title = title.replace("_", " ")
    title = re.sub(r"\s+", " ", title).strip()
    # Commons ObjectName can occasionally be generic; a concise description is better.
    if title.casefold() in {"image", "file", "untitled"} and description:
        title = description[:100]
    return title[:120]


# -------------------- Commons discovery and rights --------------------

def commons_search(session: requests.Session, search_term: str, category: str, limit: int = 24) -> list[Candidate]:
    params = {
        "action": "query",
        "format": "json",
        "formatversion": 2,
        "generator": "search",
        "gsrsearch": search_term,
        "gsrnamespace": 6,  # File namespace only
        "gsrlimit": limit,
        "prop": "imageinfo",
        "iiprop": "url|size|mime|mediatype|extmetadata",
        "iiurlwidth": 1600,
        "iiextmetadatalanguage": "en",
        "iiextmetadatafilter": "|".join([
            "LicenseShortName", "UsageTerms", "Copyrighted", "Restrictions",
            "AttributionRequired", "DeletionReason", "ObjectName", "ImageDescription",
            "DateTimeOriginal", "Artist", "Credit", "Categories",
        ]),
    }
    r = session.get(COMMONS_API, params=params, timeout=(10, 35))
    r.raise_for_status()
    payload = r.json()
    pages = (payload.get("query") or {}).get("pages") or []
    out: list[Candidate] = []

    for page in pages:
        infos = page.get("imageinfo") or []
        if not infos:
            continue
        info = infos[0]
        mime = clean_html_text(info.get("mime")).casefold()
        if mime not in {"image/png", "image/jpeg"}:
            continue
        meta = info.get("extmetadata") or {}
        license_short = ext_value(meta, "LicenseShortName")
        copyrighted = ext_value(meta, "Copyrighted")
        restrictions = ext_value(meta, "Restrictions")
        deletion = ext_value(meta, "DeletionReason")
        if not license_is_allowed(license_short, copyrighted, restrictions, deletion):
            continue

        image_url = clean_html_text(info.get("thumburl") or info.get("url"))
        if not image_url:
            continue
        description_url = clean_html_text(info.get("descriptionurl"))
        file_title = clean_html_text(page.get("title"))
        description = ext_value(meta, "ImageDescription")
        object_name = ext_value(meta, "ObjectName")
        title = normalized_display_title(file_title, object_name, description)
        artist = ext_value(meta, "Artist")
        date_text = ext_value(meta, "DateTimeOriginal")
        categories_text = ext_value(meta, "Categories")
        report = extract_report(title, description, categories_text, ext_value(meta, "Credit"), file_title)
        year = extract_year(date_text, title, description, file_title)
        width = int(info.get("width") or 0)
        height = int(info.get("height") or 0)
        if width < 350 or height < 250:
            continue

        c = Candidate(
            pageid=int(page.get("pageid") or 0),
            file_title=file_title,
            image_url=image_url,
            description_url=description_url,
            title=title,
            description=description,
            artist=artist,
            date_text=date_text,
            categories_text=categories_text,
            license_short=license_short,
            copyright_status=copyrighted,
            restrictions=restrictions,
            category=category,
            search_term=search_term,
            width=width,
            height=height,
            report=report,
            year=year,
        )
        c.score = metadata_score(c)
        out.append(c)
    return out


def metadata_score(c: Candidate) -> float:
    blob = " ".join([c.title, c.description, c.categories_text, c.file_title]).casefold()
    score = 0.0
    for term in TECHNICAL_TERMS:
        if term in blob:
            score += 10
    for term in PHOTO_TERMS:
        if term in blob:
            score -= 45
    if "naca" in blob:
        score += 24
    if "nasa" in blob:
        score += 18
    if "3-view" in blob or "three-view" in blob or "orthographic" in blob:
        score += 28
    if "schematic" in blob or "diagram" in blob:
        score += 20
    if c.report:
        score += 18
    if c.year:
        score += 5
    ratio = c.width / float(max(1, c.height))
    if 1.15 <= ratio <= 2.8:
        score += 10
    elif ratio < 0.65:
        score -= 6
    return score


def gather_category(session: requests.Session, category: str, seed: int) -> list[Candidate]:
    rng = random.Random(seed ^ sum(ord(ch) for ch in category))
    queries = SEARCHES[category][:]
    rng.shuffle(queries)
    seen: set[int] = set()
    found: list[Candidate] = []
    for query in queries:
        try:
            rows = commons_search(session, query, category, limit=24)
        except (requests.RequestException, ValueError, TypeError) as exc:
            print(f"Commons search failed [{category}] {query!r}: {exc}")
            continue
        for c in rows:
            if not c.pageid or c.pageid in seen:
                continue
            seen.add(c.pageid)
            c.score += rng.uniform(-9, 9)
            found.append(c)
        time.sleep(0.18)
    found.sort(key=lambda x: x.score, reverse=True)
    return found


# -------------------- NTRS discovery, rights, and PDF extraction --------------------

def _ntrs_first_report_number(record: dict[str, Any]) -> str:
    values: list[str] = []
    for key in ("reportNumbers", "otherReportNumbers"):
        raw = record.get(key)
        items = raw if isinstance(raw, list) else ([] if raw in (None, "") else [raw])
        for item in items:
            if isinstance(item, dict):
                value = clean_html_text(item.get("number") or item.get("reportNumber"))
            else:
                value = clean_html_text(item)
            if value:
                values.append(value)
    return values[0][:72] if values else ""


def _ntrs_year(record: dict[str, Any]) -> str:
    publications = record.get("publications") or []
    if isinstance(publications, list):
        for pub in publications:
            if isinstance(pub, dict):
                value = clean_html_text(pub.get("publicationDate"))
                m = re.search(r"\b(18\d{2}|19\d{2}|20\d{2})\b", value)
                if m:
                    return m.group(1)
    for key in ("publicationDate", "distributionDate", "submittedDate", "created"):
        value = clean_html_text(record.get(key))
        m = re.search(r"\b(18\d{2}|19\d{2}|20\d{2})\b", value)
        if m:
            return m.group(1)
    return ""


def _ntrs_pdf_link(record: dict[str, Any]) -> str:
    downloads = record.get("downloads") or []
    if isinstance(downloads, dict):
        downloads = [downloads]
    if not isinstance(downloads, list):
        return ""

    ranked: list[tuple[int, str]] = []
    for item in downloads:
        if not isinstance(item, dict) or item.get("draft") is True:
            continue
        mimetype = clean_html_text(item.get("mimetype")).casefold()
        name = clean_html_text(item.get("name")).casefold()
        if mimetype != "application/pdf" and not name.endswith(".pdf"):
            continue
        links = item.get("links") or {}
        if not isinstance(links, dict):
            continue
        for rank, key in ((0, "pdf"), (1, "original")):
            link = clean_html_text(links.get(key))
            if not link:
                continue
            if not (link.startswith("http://") or link.startswith("https://")):
                link = "https://ntrs.nasa.gov" + (link if link.startswith("/") else "/" + link)
            ranked.append((rank, link))
    if not ranked:
        return ""
    ranked.sort(key=lambda x: x[0])
    return ranked[0][1]


def _as_bool(value: object) -> bool | None:
    if isinstance(value, bool):
        return value
    text = clean_html_text(value).casefold()
    if text in {"true", "1", "yes"}:
        return True
    if text in {"false", "0", "no"}:
        return False
    return None


def ntrs_rights_allowed(record: dict[str, Any]) -> tuple[bool, str]:
    """Conservative rights gate for publishing a derived display image."""
    if clean_html_text(record.get("distribution")).upper() != "PUBLIC":
        return False, ""
    if clean_html_text(record.get("disseminated")).upper() != "DOCUMENT_AND_METADATA":
        return False, ""
    if _as_bool(record.get("downloadsAvailable")) is not True:
        return False, ""

    export = record.get("exportControl") or {}
    if not isinstance(export, dict):
        return False, ""
    for key in ("itar", "ear"):
        value = clean_html_text(export.get(key)).upper()
        if value not in {"", "NO"}:
            return False, ""

    copyright_meta = record.get("copyright") or {}
    if not isinstance(copyright_meta, dict):
        return False, ""
    determination = clean_html_text(copyright_meta.get("determinationType")).upper()
    if determination not in NTRS_RIGHTS_ALLOW:
        return False, ""
    if _as_bool(copyright_meta.get("containsThirdPartyMaterial")) is True:
        return False, ""
    third_party = clean_html_text(copyright_meta.get("thirdPartyContentCondition")).upper()
    if third_party not in {"", "NOT_SET"}:
        return False, ""
    return True, determination


def ntrs_search(session: requests.Session, query: str, category: str, limit: int = 12) -> list[NTRSRecord]:
    limit = max(1, min(int(limit), 100))
    body = {
        "q": query,
        "disseminated": "DOCUMENT_AND_METADATA",
        "page": {"size": limit, "from": 0},
    }
    url = f"{NTRS_API}/citations/search"
    try:
        response = session.post(url, json=body, timeout=(10, 40))
        response.raise_for_status()
    except requests.RequestException as post_exc:
        params = {
            "q": query,
            "disseminated": "DOCUMENT_AND_METADATA",
            "page.size": limit,
            "page.from": 0,
        }
        try:
            response = session.get(url, params=params, timeout=(10, 40))
            response.raise_for_status()
        except requests.RequestException:
            raise post_exc

    payload = response.json()
    rows = payload.get("results", []) if isinstance(payload, dict) else []
    if not isinstance(rows, list):
        raise RuntimeError("NTRS search returned an unexpected response shape")

    out: list[NTRSRecord] = []
    for record in rows:
        if not isinstance(record, dict):
            continue
        allowed, rights = ntrs_rights_allowed(record)
        if not allowed:
            continue
        pdf_url = _ntrs_pdf_link(record)
        if not pdf_url:
            continue
        record_id = clean_html_text(record.get("id"))
        if not record_id or not re.fullmatch(r"\d+", record_id):
            continue
        title = clean_html_text(record.get("title")) or f"NASA report {record_id}"
        abstract = clean_html_text(record.get("abstract"))
        subjects = clean_html_text(record.get("subjectCategories"))
        report = _ntrs_first_report_number(record)
        year = _ntrs_year(record)
        blob = f"{title} {abstract} {subjects} {report}".casefold()
        score = 0.0
        for term in NTRS_PAGE_TERMS.get(category, ()):
            if term.casefold() in blob:
                score += 12
        if "naca" in blob:
            score += 20
        if "nasa" in blob:
            score += 8
        if report:
            score += 8
        out.append(NTRSRecord(
            record_id=record_id,
            title=title,
            abstract=abstract,
            category=category,
            report=report,
            year=year,
            citation_url=f"https://ntrs.nasa.gov/citations/{record_id}",
            pdf_url=pdf_url,
            rights=rights,
            subject_text=subjects,
            score=score,
        ))
    out.sort(key=lambda x: x.score, reverse=True)
    return out


def _ntrs_page_text(page: Any) -> str:
    try:
        textpage = page.get_textpage()
        try:
            return clean_html_text(textpage.get_text_bounded())
        finally:
            textpage.close()
    except Exception:
        return ""


def _ntrs_page_score(gray: Image.Image, text: str, category: str) -> float:
    """Score for display-worthiness; do not reward nearly blank title pages."""
    m = image_metrics(gray)
    nonwhite = max(0.0, 1.0 - m["white"])
    score = 0.0
    if nonwhite < 0.010:
        score -= 120
    elif nonwhite < 0.025:
        score -= 45
    elif nonwhite <= 0.30:
        score += 35
    else:
        score -= (nonwhite - 0.30) * 180

    score += min(m["edge"] / 22.0, 1.8) * 48
    score += min(m["std"] / 55.0, 1.5) * 26
    if m["edge"] < 2.0:
        score -= 55
    if m["std"] < 8.0:
        score -= 45
    if m["mean"] < 145:
        score -= (145 - m["mean"]) * 1.5
    if m["dark"] > 0.25:
        score -= (m["dark"] - 0.25) * 260

    folded = text.casefold()
    signal_terms = 0
    for term in NTRS_PAGE_TERMS.get(category, ()):
        if term.casefold() in folded:
            score += 16
            signal_terms += 1
    if "figure" in folded:
        score += 10
        signal_terms += 1
    if "schematic" in folded or "configuration" in folded or "drawing" in folded:
        score += 12
        signal_terms += 1

    chars = len(text)
    if chars > 3600:
        score -= 150
    elif chars > 2400:
        score -= 90
    elif chars > 1500:
        score -= 45
    elif chars > 900:
        score -= 15
    if signal_terms == 0 and m["edge"] < 5.0:
        score -= 35
    return score


def _download_pdf(session: requests.Session, record: NTRSRecord, budget: NTRSBudget) -> bytes:
    if not budget.can_attempt():
        raise RuntimeError("NTRS daily PDF budget exhausted")
    budget.note_attempt()
    with session.get(record.pdf_url, timeout=(10, 75), stream=True) as response:
        response.raise_for_status()
        try:
            declared = int(response.headers.get("Content-Length") or 0)
        except (TypeError, ValueError):
            declared = 0
        if declared and declared > NTRS_MAX_PDF_BYTES:
            raise RuntimeError(f"NTRS PDF too large ({declared / 1048576:.1f} MB)")
        if declared and budget.bytes_downloaded + declared > NTRS_MAX_PDF_BYTES_PER_DAY:
            raise RuntimeError("NTRS daily byte budget would be exceeded")

        data = bytearray()
        for chunk in response.iter_content(1024 * 256):
            if not chunk:
                continue
            data.extend(chunk)
            budget.note_bytes(len(chunk))
            if len(data) > NTRS_MAX_PDF_BYTES:
                raise RuntimeError("NTRS PDF exceeded per-file size limit")
            if budget.bytes_downloaded > NTRS_MAX_PDF_BYTES_PER_DAY:
                raise RuntimeError("NTRS daily byte budget exceeded")

    if not data:
        raise RuntimeError("NTRS returned an empty PDF")
    if not bytes(data[:1024]).lstrip().startswith(b"%PDF-"):
        raise RuntimeError("NTRS download was not a PDF")
    return bytes(data)


def _distributed_indices(page_count: int, cap: int) -> list[int]:
    if page_count <= 0 or cap <= 0:
        return []
    if page_count <= cap:
        return list(range(page_count))

    # Reserve only part of the budget for early pages; spread the rest across
    # the full document. This matters for small caps too (e.g. 8 fallbacks).
    early_count = min(page_count, min(18, max(2, cap // 4)))
    early = list(range(early_count))
    remaining = max(0, cap - len(early))
    sampled = [
        round((page_count - 1) * i / max(1, remaining - 1))
        for i in range(remaining)
    ] if remaining else []
    out: list[int] = []
    for idx in early + sampled:
        if 0 <= idx < page_count and idx not in out:
            out.append(idx)
    # Rounding/duplicates can make the list short. Fill deterministically.
    if len(out) < cap:
        for idx in range(page_count):
            if idx not in out:
                out.append(idx)
                if len(out) >= cap:
                    break
    return out[:cap]


def extract_best_ntrs_page(session: requests.Session, record: NTRSRecord, budget: NTRSBudget) -> Candidate | None:
    data = _download_pdf(session, record, budget)
    pdf = pdfium.PdfDocument(data)
    try:
        page_count = len(pdf)
        if page_count <= 0:
            return None

        text_scores: list[tuple[float, int, str]] = []
        inspect_indices = _distributed_indices(page_count, NTRS_MAX_PAGES_TO_INSPECT)
        for idx in inspect_indices:
            page = pdf[idx]
            try:
                page_text = _ntrs_page_text(page)
            finally:
                page.close()
            folded = page_text.casefold()
            s_text = 0.0
            for term in NTRS_PAGE_TERMS.get(record.category, ()):
                if term.casefold() in folded:
                    s_text += 10
            if "figure" in folded:
                s_text += 4
            if "schematic" in folded or "drawing" in folded or "configuration" in folded:
                s_text += 4
            if len(page_text) > 3000:
                s_text -= 20
            text_scores.append((s_text, idx, page_text))

        text_scores.sort(key=lambda item: item[0], reverse=True)

        # Balance text-guided pages with a broad document-wide sample. Some old
        # NACA/NASA reports are image-only scans with little or no extractable
        # text; selecting only the highest text scores would then bias heavily
        # toward the first pages and can miss excellent drawings later on.
        candidate_indices: list[int] = []
        text_guided_cap = max(1, NTRS_MAX_PAGES_TO_RENDER // 2)
        for text_score, idx, _ in text_scores:
            if text_score <= 0:
                continue
            if idx not in candidate_indices:
                candidate_indices.append(idx)
            if len(candidate_indices) >= text_guided_cap:
                break

        for idx in _distributed_indices(page_count, NTRS_MAX_PAGES_TO_RENDER):
            if idx not in candidate_indices:
                candidate_indices.append(idx)
            if len(candidate_indices) >= NTRS_MAX_PAGES_TO_RENDER:
                break

        # If duplicate sampling left room, fill from the remaining text-ranked
        # pages regardless of score. This keeps the render count bounded while
        # avoiding gaps on short or oddly structured reports.
        if len(candidate_indices) < NTRS_MAX_PAGES_TO_RENDER:
            for _, idx, _ in text_scores:
                if idx not in candidate_indices:
                    candidate_indices.append(idx)
                if len(candidate_indices) >= NTRS_MAX_PAGES_TO_RENDER:
                    break

        text_by_index = {idx: txt for _, idx, txt in text_scores}

        best: tuple[float, float, int, Image.Image, str] | None = None
        for idx in candidate_indices:
            page = pdf[idx]
            try:
                page_w, page_h = page.get_size()
                if page_w <= 0 or page_h <= 0:
                    continue
                target_scale = max(0.75, min(2.0, 1350.0 / page_w))
                max_scale_by_pixels = math.sqrt(NTRS_MAX_RENDER_PIXELS / max(1.0, page_w * page_h))
                scale = min(target_scale, max_scale_by_pixels)
                # Extremely large foldouts can otherwise explode memory usage.
                if scale < 0.20:
                    continue
                bitmap = page.render(scale=scale, rotation=0)
                try:
                    pil = bitmap.to_pil().convert("RGB").copy()
                finally:
                    bitmap.close()
            finally:
                page.close()

            gray = crop_scan_border(ImageOps.grayscale(pil))
            text_here = text_by_index.get(idx, "")
            page_score = _ntrs_page_score(gray, text_here, record.category)
            total_score = record.score + page_score
            if page_score < 12:
                continue
            if best is None or total_score > best[0]:
                best = (total_score, page_score, idx, gray, text_here)

        if best is None:
            return None
        score, page_score, page_index, gray, page_text = best
        synthetic_pageid = -(int(record.record_id) * 1000 + page_index + 1)
        candidate = Candidate(
            pageid=synthetic_pageid,
            file_title=f"NTRS:{record.record_id}:page:{page_index + 1}",
            image_url="",
            description_url=record.citation_url,
            title=record.title[:120],
            description=record.abstract,
            artist="NASA / NACA",
            date_text=record.year,
            categories_text=record.subject_text,
            license_short=record.rights.replace("_", " ").title(),
            copyright_status="False",
            restrictions="",
            category=record.category,
            search_term="NTRS",
            width=gray.width,
            height=gray.height,
            source_kind="ntrs",
            source_name=NTRS_SOURCE_NAME,
            source_id=record.record_id,
            pdf_page=page_index + 1,
            report=record.report,
            year=record.year,
            score=score,
        )
        frame, ink = render_frame(candidate, gray)
        if not (0.020 <= ink <= 0.235):
            return None
        candidate.frame = frame
        candidate.ink = ink
        return candidate
    finally:
        pdf.close()


def gather_ntrs_feature(
    session: requests.Session,
    category: str,
    seed: int,
    budget: NTRSBudget,
    used_record_ids: set[str],
) -> Candidate | None:
    if not budget.can_attempt():
        return None
    rng = random.Random(seed ^ 0x4E545253 ^ sum(ord(ch) for ch in category))
    queries = list(NTRS_SEARCHES.get(category, ()))
    rng.shuffle(queries)
    seen: set[str] = set()
    records: list[NTRSRecord] = []
    for query in queries[:3]:
        try:
            for record in ntrs_search(session, query, category, limit=10):
                if record.record_id in seen or record.record_id in used_record_ids:
                    continue
                seen.add(record.record_id)
                record.score += rng.uniform(-5, 5)
                records.append(record)
        except Exception as exc:
            print(f"NTRS search failed [{category}] {query!r}: {exc}")
        time.sleep(0.20)
    records.sort(key=lambda x: x.score, reverse=True)

    for record in records[:NTRS_MAX_PDF_ATTEMPTS_PER_CATEGORY]:
        if not budget.can_attempt():
            break
        try:
            candidate = extract_best_ntrs_page(session, record, budget)
            if candidate is not None:
                used_record_ids.add(record.record_id)
                print(
                    f"[{category}] NTRS page={candidate.pdf_page} ink={candidate.ink*100:4.1f}% "
                    f"rights={candidate.license_short!r} {candidate.title}"
                )
                return candidate
        except Exception as exc:
            print(f"NTRS PDF failed {record.record_id}: {exc}")
        time.sleep(0.25)
    return None


# -------------------- Image processing --------------------

def download_image(session: requests.Session, c: Candidate) -> Image.Image:
    r = session.get(c.image_url, timeout=(10, 40), headers={"Accept": "image/*,*/*;q=0.8"})
    r.raise_for_status()
    if not r.content:
        raise RuntimeError("empty image response")
    im = Image.open(io.BytesIO(r.content))
    im.load()
    return ImageOps.exif_transpose(im).convert("RGB")


def crop_scan_border(gray: Image.Image) -> Image.Image:
    im = gray.convert("L")
    w, h = im.size
    if w < 30 or h < 30:
        return im
    # Estimate the paper from the four corners.
    cw, ch = max(5, w // 18), max(5, h // 18)
    corners = [
        im.crop((0, 0, cw, ch)), im.crop((w - cw, 0, w, ch)),
        im.crop((0, h - ch, cw, h)), im.crop((w - cw, h - ch, w, h)),
    ]
    bg = statistics.mean(ImageStat.Stat(x).mean[0] for x in corners)
    if bg < 170:
        return im
    threshold = max(190, min(246, int(bg - 13)))
    mask = im.point(lambda p: 255 if p < threshold else 0)
    bbox = mask.getbbox()
    if not bbox:
        return im
    l, t, r, b = bbox
    pad_x = max(4, int((r - l) * 0.025))
    pad_y = max(4, int((b - t) * 0.025))
    box = (max(0, l - pad_x), max(0, t - pad_y), min(w, r + pad_x), min(h, b + pad_y))
    cropped = im.crop(box)
    if cropped.width * cropped.height < 0.20 * w * h:
        return im
    return cropped


def image_metrics(gray: Image.Image) -> dict[str, float]:
    small = gray.convert("L").resize((220, 150), Image.Resampling.BILINEAR)
    stat = ImageStat.Stat(small)
    hist = small.histogram()
    n = small.width * small.height
    edges = small.filter(ImageFilter.FIND_EDGES)
    return {
        "mean": stat.mean[0],
        "std": stat.stddev[0],
        "white": sum(hist[225:]) / n,
        "dark": sum(hist[:75]) / n,
        "edge": ImageStat.Stat(edges).mean[0],
    }


def visual_suitability(c: Candidate, gray: Image.Image) -> float:
    m = image_metrics(gray)
    blob = " ".join([c.title, c.description, c.categories_text]).casefold()
    score = c.score
    score += min(m["white"], 0.85) * 70
    score += min(m["edge"] / 28.0, 1.6) * 45
    score += min(m["std"] / 60.0, 1.4) * 25
    if m["mean"] < 145:
        score -= (145 - m["mean"]) * 1.6
    if m["dark"] > 0.27:
        score -= (m["dark"] - 0.27) * 240
    if m["white"] < 0.14:
        score -= 55
    if any(x in blob for x in PHOTO_TERMS):
        score -= 50
    return score


def technical_line_art(gray: Image.Image) -> Image.Image:
    g = ImageOps.autocontrast(gray.convert("L"), cutoff=(0.25, 0.25))
    g = ImageEnhance.Contrast(g).enhance(1.15)
    g = g.filter(ImageFilter.UnsharpMask(radius=0.85, percent=115, threshold=3))
    # Adaptive background threshold keeps fine black linework while whitening paper.
    local = g.filter(ImageFilter.GaussianBlur(radius=5.0))
    a = g.tobytes()
    b = local.tobytes()
    out = bytearray(len(a))
    for i, (p, bg) in enumerate(zip(a, b)):
        threshold = max(105, min(206, bg - 42))
        out[i] = 255 if p >= threshold else 0
    return Image.frombytes("L", g.size, bytes(out)).convert("1", dither=Image.Dither.NONE)


def atkinson(gray: Image.Image, threshold: float = 116.0) -> Image.Image:
    g = ImageOps.autocontrast(gray.convert("L"), cutoff=(0.25, 0.25))
    g = ImageEnhance.Contrast(g).enhance(1.08)
    # Mild gamma lift.
    lut = [max(0, min(255, round(255 * ((i / 255.0) ** 0.80) + 4))) for i in range(256)]
    g = g.point(lut)
    w, h = g.size
    px = [float(v) for v in g.tobytes()]
    for y in range(h):
        row = y * w
        for x in range(w):
            i = row + x
            old = px[i]
            new = 255.0 if old >= threshold else 0.0
            px[i] = new
            err = (old - new) / 8.0
            for dx, dy in ((1, 0), (2, 0), (-1, 1), (0, 1), (1, 1), (0, 2)):
                nx, ny = x + dx, y + dy
                if 0 <= nx < w and 0 <= ny < h:
                    j = ny * w + nx
                    px[j] = min(255.0, max(0.0, px[j] + err))
    data = bytes(255 if v >= threshold else 0 for v in px)
    return Image.frombytes("L", (w, h), data).convert("1", dither=Image.Dither.NONE)


def black_fraction(im: Image.Image) -> float:
    hist = im.convert("1").histogram()
    return (hist[0] if hist else 0) / float(max(1, im.width * im.height))


def font(size: int, bold: bool = False) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    names = [
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf" if bold else "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        "DejaVuSans-Bold.ttf" if bold else "DejaVuSans.ttf",
    ]
    for name in names:
        try:
            return ImageFont.truetype(name, size)
        except OSError:
            pass
    try:
        return ImageFont.load_default(size=size)
    except TypeError:
        return ImageFont.load_default()


def ellipsize(draw: ImageDraw.ImageDraw, text: str, fnt: ImageFont.ImageFont, max_width: int) -> str:
    text = clean_html_text(text)
    if draw.textbbox((0, 0), text, font=fnt)[2] <= max_width:
        return text
    suffix = "…"
    lo, hi = 0, len(text)
    while lo < hi:
        mid = (lo + hi + 1) // 2
        candidate = text[:mid].rstrip() + suffix
        if draw.textbbox((0, 0), candidate, font=fnt)[2] <= max_width:
            lo = mid
        else:
            hi = mid - 1
    return text[:lo].rstrip() + suffix


def caption_source_line(c: Candidate) -> str:
    pieces = []
    if c.report:
        pieces.append(c.report)
    else:
        pieces.append(c.source_name)
    if c.year:
        pieces.append(c.year)
    return " · ".join(pieces)


def render_frame(c: Candidate, gray: Image.Image) -> tuple[Image.Image, float]:
    # Fit the complete drawing into the artwork area. Technical drawings can be
    # portrait-oriented, so preserve all content rather than cropping off views.
    art = crop_scan_border(gray)
    max_w, max_h = W - 18, ART_H - 12
    scale = min(max_w / art.width, max_h / art.height)
    new_size = (max(1, round(art.width * scale)), max(1, round(art.height * scale)))
    art = art.resize(new_size, Image.Resampling.LANCZOS)

    m = image_metrics(art)
    # Strongly line-based scans use adaptive threshold; denser diagrams get
    # Atkinson so shaded regions still read on the monochrome panel.
    if m["white"] > 0.34 or m["edge"] > 12:
        bw_art = technical_line_art(art)
        mode = "line"
    else:
        bw_art = atkinson(art)
        mode = "atkinson"

    canvas = Image.new("1", (W, H), 1)
    x = (W - bw_art.width) // 2
    y = max(3, (ART_H - bw_art.height) // 2)
    canvas.paste(bw_art, (x, y))

    # Caption is rendered on a grayscale strip then thresholded, for crisp text.
    cap = Image.new("L", (W, CAPTION_H), 255)
    d = ImageDraw.Draw(cap)
    title_font = font(17, bold=True)
    meta_font = font(13, bold=False)
    title = ellipsize(d, c.title, title_font, W - 24)
    meta = ellipsize(d, caption_source_line(c), meta_font, W - 24)
    d.line((10, 1, W - 10, 1), fill=70, width=1)
    d.text((12, 9), title, font=title_font, fill=0)
    d.text((12, 37), meta, font=meta_font, fill=30)
    cap1 = cap.point(lambda p: 255 if p >= 145 else 0).convert("1")
    canvas.paste(cap1, (0, ART_H))

    ink = black_fraction(canvas)
    c.score += 3 if mode == "line" else 0
    return canvas, ink


def pack_1bpp(img1: Image.Image) -> bytes:
    im = img1.convert("1")
    if im.size != (W, H):
        raise ValueError(f"frame must be {W}x{H}")
    px = im.load()
    out = bytearray(FRAME_BYTES)
    k = 0
    for y in range(H):
        for x0 in range(0, W, 8):
            v = 0
            for bit in range(8):
                if px[x0 + bit, y] != 0:
                    v |= 1 << (7 - bit)
            out[k] = v
            k += 1
    return bytes(out)


# -------------------- Selection --------------------

def evaluate_category(
    session: requests.Session,
    category: str,
    candidates: list[Candidate],
    quota: int,
    globally_used: set[int],
) -> list[Candidate]:
    if quota <= 0:
        return []
    accepted: list[Candidate] = []
    # Try deeper than quota because rights-safe search results can still be photos
    # or poor 1-bit candidates.
    for c in candidates[: max(22, quota * 7)]:
        if c.pageid in globally_used:
            continue
        try:
            image = download_image(session, c)
            gray = crop_scan_border(ImageOps.grayscale(image))
            metrics = image_metrics(gray)
            if metrics["mean"] < 118 or metrics["dark"] > 0.40:
                continue
            c.score = visual_suitability(c, gray)
            frame, ink = render_frame(c, gray)
            c.ink = ink
            # Caption contributes some ink. Reject extremely blank or black frames.
            if ink < 0.020 or ink > 0.235:
                continue
            c.frame = frame
            accepted.append(c)
            print(
                f"[{category}] score={c.score:6.1f} ink={ink*100:4.1f}% "
                f"lic={c.license_short!r} {c.title}"
            )
        except Exception as exc:
            print(f"image failed {c.file_title}: {exc}")
        time.sleep(0.18)

    accepted.sort(key=lambda x: x.score, reverse=True)
    selected = accepted[:quota]
    globally_used.update(c.pageid for c in selected)
    return selected


def build_selection(session: requests.Session, date_str: str, quota: int) -> list[Candidate]:
    seed = int(date_str.replace("-", ""))
    globally_used: set[int] = set()
    selected: list[Candidate] = []

    budget = NTRSBudget()
    used_ntrs_records: set[str] = set()
    ntrs_by_category: dict[str, Candidate] = {}
    ntrs_order = CATEGORY_ORDER[:]
    random.Random(seed ^ 0x4E545253).shuffle(ntrs_order)
    for category in ntrs_order:
        if len(ntrs_by_category) >= NTRS_MAX_FEATURES_PER_DAY or not budget.can_attempt():
            break
        try:
            candidate = gather_ntrs_feature(session, category, seed, budget, used_ntrs_records)
        except Exception as exc:
            # NTRS is an enrichment source, never a hard dependency. If NASA's
            # API/PDF service changes unexpectedly, keep building the reliable
            # Commons-backed feed rather than failing the entire daily gallery.
            print(f"NTRS category failed [{category}]: {exc}")
            candidate = None
        if candidate is not None:
            ntrs_by_category[category] = candidate

    print(
        f"NTRS daily budget: {budget.attempts} PDF attempts, "
        f"{budget.bytes_downloaded / 1048576:.1f} MB downloaded, "
        f"{len(ntrs_by_category)} selected"
    )

    for category in CATEGORY_ORDER:
        category_selected: list[Candidate] = []
        ntrs_candidate = ntrs_by_category.get(category)
        if ntrs_candidate is not None and ntrs_candidate.pageid not in globally_used:
            category_selected.append(ntrs_candidate)
            globally_used.add(ntrs_candidate.pageid)

        commons_candidates = gather_category(session, category, seed)
        commons_quota = max(0, quota - len(category_selected))
        chosen_commons = evaluate_category(session, category, commons_candidates, commons_quota, globally_used)
        category_selected.extend(chosen_commons)
        selected.extend(category_selected)

        if len(category_selected) < quota:
            print(f"WARNING: {CATEGORY_LABELS[category]} produced {len(category_selected)}/{quota} works")

    if not selected:
        raise RuntimeError("No rights-cleared aerospace drawings could be selected")
    return selected


# -------------------- Feed/site output --------------------

def validate_date(date_str: str | None) -> str:
    if not date_str:
        return dt.date.today().isoformat()
    return dt.date.fromisoformat(date_str).isoformat()


def clear_old_outputs(out: Path) -> None:
    if not out.exists():
        return
    for p in out.iterdir():
        if p.is_file() and re.fullmatch(r"slot\d+\.(?:bin|txt|png)", p.name):
            p.unlink()
        elif p.name in {"index.txt", "index.json"} and p.is_file():
            p.unlink()


def write_slot(out: Path, pack: str, slot: int, total: int, c: Candidate) -> dict[str, Any]:
    if c.frame is None:
        raise RuntimeError(f"candidate {c.id} has no rendered frame")
    packed = pack_1bpp(c.frame)
    crc = binascii.crc32(packed) & 0xFFFFFFFF
    (out / f"slot{slot}.bin").write_bytes(packed)
    c.frame.convert("L").save(out / f"slot{slot}.png", optimize=True)

    lines = [
        "PG1",
        f"pack={pack}",
        f"slot={slot}",
        f"count={total}",
        f"id={c.id}",
        f"title={c.title}",
        f"artist={clean_html_text(c.artist) or 'NASA / NACA / aerospace source'}",
        f"date={c.year}",
        f"museum={c.source_name}",
        f"source={c.description_url}",
        f"category={c.category}",
        f"category_label={CATEGORY_LABELS[c.category]}",
        f"report={c.report}",
        f"license={c.license_short}",
        f"ink={c.ink * 100:.1f}",
        f"bytes={len(packed)}",
        f"crc32={crc:08X}",
    ]
    (out / f"slot{slot}.txt").write_text("\n".join(lines) + "\n", encoding="utf-8")
    return {
        "slot": slot,
        "id": c.id,
        "title": c.title,
        "category": c.category,
        "category_label": CATEGORY_LABELS[c.category],
        "report": c.report,
        "year": c.year,
        "source": c.description_url,
        "source_name": c.source_name,
        "source_kind": c.source_kind,
        "pdf_page": c.pdf_page,
        "license": c.license_short,
        "ink": round(c.ink * 100, 1),
        "crc32": f"{crc:08X}",
    }


def write_gallery(site: Path, pack: str, works: list[dict[str, Any]]) -> None:
    site.mkdir(parents=True, exist_ok=True)
    groups: dict[str, list[dict[str, Any]]] = {c: [] for c in CATEGORY_ORDER}
    for w in works:
        groups.setdefault(str(w["category"]), []).append(w)

    sections = []
    for cat in CATEGORY_ORDER:
        cards = []
        for w in groups.get(cat, []):
            slot = int(w["slot"])
            title = html.escape(str(w["title"]))
            source = html.escape(str(w["source"]), quote=True)
            report = html.escape(str(w.get("report") or w.get("source_name") or SOURCE_NAME))
            year = html.escape(str(w.get("year") or ""))
            lic = html.escape(str(w.get("license") or ""))
            source_name = html.escape(str(w.get("source_name") or SOURCE_NAME))
            page_no = int(w.get("pdf_page") or 0)
            crc = html.escape(str(w.get("crc32") or ""))
            cards.append(
                f'<article><img src="../feed/aerospace/slot{slot}.png?v={crc}" alt="{title}">'
                f'<h3>{title}</h3><p>{report}{(" · " + year) if year else ""}</p>'
                f'<p class="muted">{source_name}{(" · PDF p. " + str(page_no)) if page_no else ""} · {lic} · {w.get("ink", "?")}% ink</p>'
                f'<a href="{source}" rel="noopener">Source record</a></article>'
            )
        sections.append(f"<section><h2>{html.escape(CATEGORY_LABELS[cat])}</h2><div class='grid'>{''.join(cards) or '<p>No works selected today.</p>'}</div></section>")

    page = f"""<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Paper Gallery Aerospace</title><style>
body{{font-family:system-ui,sans-serif;background:#eef0ed;color:#151515;margin:0}}main{{max-width:1180px;margin:auto;padding:28px 18px 60px}}
h1{{font-family:Georgia,serif;font-weight:500;font-size:46px}}h2{{margin-top:42px;border-bottom:1px solid #aaa;padding-bottom:8px}}
.grid{{display:grid;grid-template-columns:repeat(auto-fit,minmax(300px,1fr));gap:20px}}article{{background:#fff;border:1px solid #c9ccc7;padding:12px}}
img{{width:100%;height:auto;background:white;border:1px solid #eee}}h3{{font-family:Georgia,serif;font-weight:500}}.muted{{color:#666}}a{{color:#111}}
</style></head><body><main><p>PAPER GALLERY</p><h1>Aerospace Drawings</h1>
<p>Pack {html.escape(pack)} · Wikimedia Commons Public Domain/CC0 plus rights-checked NASA NTRS public-use material.</p>{''.join(sections)}</main></body></html>"""
    (site / "index.html").write_text(page, encoding="utf-8")


def generate(output: Path, site: Path, count_per_category: int, date_str: str | None) -> None:
    pack = validate_date(date_str)
    output.mkdir(parents=True, exist_ok=True)
    clear_old_outputs(output)
    session = make_session()
    selected = build_selection(session, pack, count_per_category)
    works = [write_slot(output, pack, i, len(selected), c) for i, c in enumerate(selected)]

    category_ranges: dict[str, dict[str, int]] = {}
    for cat in CATEGORY_ORDER:
        slots = [i for i, c in enumerate(selected) if c.category == cat]
        if slots:
            category_ranges[cat] = {"start": min(slots), "count": len(slots)}
        else:
            category_ranges[cat] = {"start": -1, "count": 0}

    index_lines = ["PG1", f"pack={pack}", f"count={len(selected)}", "mode=aerospace"]
    for cat in CATEGORY_ORDER:
        r = category_ranges[cat]
        index_lines.append(f"category_{cat}={r['start']},{r['count']}")
    (output / "index.txt").write_text("\n".join(index_lines) + "\n", encoding="utf-8")
    (output / "index.json").write_text(
        json.dumps({
            "pack": pack,
            "mode": "aerospace",
            "count": len(selected),
            "category_ranges": category_ranges,
            "works": works,
        }, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    write_gallery(site, pack, works)
    print(f"Wrote {len(selected)} Aerospace frames to {output}")


# -------------------- Offline self-test --------------------

def self_test() -> None:
    assert license_is_allowed("Public domain", "False", "", "")
    assert license_is_allowed("CC0 1.0", "False", "", "")
    assert not license_is_allowed("CC BY-SA 4.0", "False", "", "")
    assert not license_is_allowed("Public domain", "True", "", "")
    assert not license_is_allowed("Public domain", "False", "trademark", "")
    assert clean_html_text([]) == ""
    assert extract_report("Fokker three-view NACA-AC-187") == "NACA-AC-187"
    assert extract_year("1 February 1934") == "1934"

    ntrs_fake = {
        "id": 19860005739,
        "distribution": "PUBLIC",
        "disseminated": "DOCUMENT_AND_METADATA",
        "downloadsAvailable": True,
        "copyright": {
            "determinationType": "GOV_PUBLIC_USE_PERMITTED",
            "thirdPartyContentCondition": "NOT_SET",
            "containsThirdPartyMaterial": False,
        },
        "exportControl": {"itar": "NO", "ear": "NO"},
        "downloads": [{
            "draft": False,
            "mimetype": "application/pdf",
            "links": {"pdf": "/api/citations/19860005739/downloads/test.pdf"},
        }],
        "reportNumbers": ["NASA-TM-87500"],
        "publications": [{"publicationDate": "1984-07-01"}],
    }
    allowed, rights = ntrs_rights_allowed(ntrs_fake)
    assert allowed and rights == "GOV_PUBLIC_USE_PERMITTED"
    assert _ntrs_pdf_link(ntrs_fake).startswith("https://ntrs.nasa.gov/")
    assert _ntrs_first_report_number(ntrs_fake) == "NASA-TM-87500"
    assert _ntrs_year(ntrs_fake) == "1984"
    blocked = dict(ntrs_fake)
    blocked["copyright"] = dict(ntrs_fake["copyright"], containsThirdPartyMaterial=True)
    assert not ntrs_rights_allowed(blocked)[0]

    fake = Candidate(
        pageid=123,
        file_title="File:Test NACA-AC-187.png",
        image_url="https://example.invalid/test.png",
        description_url="https://commons.wikimedia.org/wiki/File:Test",
        title="Experimental Aircraft Three-View",
        description="NACA technical drawing",
        artist="NACA",
        date_text="1934",
        categories_text="3-view aircraft|PD NASA",
        license_short="Public domain",
        copyright_status="False",
        restrictions="",
        category="aircraft-three-views",
        search_term="test",
        width=1600,
        height=900,
        report="NACA-AC-187",
        year="1934",
    )
    assert metadata_score(fake) > 40

    # Synthetic line drawing with three aircraft-view-like shapes.
    img = Image.new("L", (1400, 780), 255)
    d = ImageDraw.Draw(img)
    for y in (130, 360, 590):
        d.ellipse((220, y - 35, 1180, y + 35), outline=0, width=3)
        d.line((700, y - 90, 700, y + 90), fill=0, width=3)
    frame, ink = render_frame(fake, img)
    fake.frame = frame
    fake.ink = ink
    assert frame.size == (W, H)
    packed = pack_1bpp(frame)
    assert len(packed) == FRAME_BYTES
    assert 0.01 < ink < 0.20

    # PDFium smoke test: the NTRS phase depends on rendering report pages.
    pdf_buf = io.BytesIO()
    pdf_source = Image.new("RGB", (640, 480), "white")
    pd = ImageDraw.Draw(pdf_source)
    pd.rectangle((80, 120, 560, 360), outline="black", width=4)
    pd.line((80, 240, 560, 240), fill="black", width=3)
    pdf_source.save(pdf_buf, format="PDF", resolution=120)
    test_pdf = pdfium.PdfDocument(pdf_buf.getvalue())
    try:
        test_page = test_pdf[0]
        try:
            test_bitmap = test_page.render(scale=1.0)
            try:
                rendered_pdf_page = test_bitmap.to_pil().convert("L").copy()
            finally:
                test_bitmap.close()
        finally:
            test_page.close()
    finally:
        test_pdf.close()
    assert rendered_pdf_page.width > 100 and rendered_pdf_page.height > 100
    assert _ntrs_page_score(rendered_pdf_page, "Figure 3. Aircraft configuration", "aircraft-three-views") > 0


    # Robust parser and page-selection tests.
    scalar_report = dict(ntrs_fake)
    scalar_report["otherReportNumbers"] = "NASA-TM-99999"
    scalar_report.pop("reportNumbers", None)
    assert _ntrs_first_report_number(scalar_report) == "NASA-TM-99999"
    string_false = dict(ntrs_fake)
    string_false["downloadsAvailable"] = "false"
    assert not ntrs_rights_allowed(string_false)[0]
    assert max(_distributed_indices(300, 40)) > 200
    assert len(_distributed_indices(300, 40)) == 40
    assert max(_distributed_indices(300, 8)) > 200
    assert len(_distributed_indices(300, 8)) == 8

    blank_page = Image.new("L", (1000, 1400), 255)
    technical_page = Image.new("L", (1000, 1400), 255)
    td = ImageDraw.Draw(technical_page)
    for yy in (350, 700, 1050):
        td.rectangle((180, yy - 70, 820, yy + 70), outline=0, width=5)
        td.line((500, yy - 150, 500, yy + 150), fill=0, width=4)
    assert _ntrs_page_score(technical_page, "Figure 4. Aircraft configuration", "aircraft-three-views") > _ntrs_page_score(blank_page, "Title", "aircraft-three-views")

    # Bit polarity: first black pixel, remaining first byte white.
    test = Image.new("1", (W, H), 1)
    test.putpixel((0, 0), 0)
    assert pack_1bpp(test)[0] == 0x7F

    print("Paper Gallery Aerospace generator self-test passed")


def main() -> None:
    parser = argparse.ArgumentParser(description="Build Paper Gallery Aerospace Drawings feed")
    parser.add_argument("--output", default="site/feed/aerospace", help="Aerospace feed output directory")
    parser.add_argument("--site", default="site/aerospace", help="Aerospace preview site directory")
    parser.add_argument("--count-per-category", type=int, default=4, help="Target works per aerospace category")
    parser.add_argument("--date", help="Override YYYY-MM-DD pack date")
    parser.add_argument("--self-test", action="store_true")
    args = parser.parse_args()
    if args.self_test:
        self_test()
        return
    if not (1 <= args.count_per_category <= 8):
        parser.error("--count-per-category must be 1..8")
    generate(Path(args.output), Path(args.site), args.count_per_category, args.date)


if __name__ == "__main__":
    main()
