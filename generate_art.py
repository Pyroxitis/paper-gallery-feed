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
