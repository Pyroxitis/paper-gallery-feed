# Paper Gallery

Paper Gallery turns the Seeed Studio TRMNL 7.5" (OG) DIY Kit into a low-power daily public-domain art frame.

## What this V1 does

- Uses the existing XIAO ESP32-S3 Plus + 7.5" 800×480 UC8179 display.
- Preserves low-power deep sleep, the three hardware buttons, local mDNS page, battery monitoring, and one-minute awake window.
- Downloads a preprocessed **48,000-byte 1-bit frame** instead of decoding/dithering large museum images on the ESP32.
- Updates once per day at a local scheduled time.
- **D1:** next artwork in the day's pack.
- **D2:** re-check/reselect from the current daily pack.
- **D4:** wake / keep the local page available.
- Keeps the old e-paper image if Wi-Fi or the feed fails.
- Avoids recently displayed artwork IDs when a new daily pack arrives.
- Local page: `http://paper-gallery.local` (user `paper`, PIN `2684`).

## Art source

V1 deliberately uses **Art Institute of Chicago only** and requests records with `is_public_domain=true`. The generator strongly favors etchings, engravings, woodcuts, ink/graphite drawings, architectural and botanical works, landscapes, and related line-based material. The museum's IIIF image service is used at its recommended 843-pixel size.

## Folder layout

- `firmware/PaperGallery/PaperGallery.ino` — ESP32 firmware.
- `firmware/PaperGallery/driver.h` — TRMNL 7.5" / EE04 / UC8179 display selection.
- `firmware/PaperGallery/config.h` — feed URL, PIN, timezone, and daily schedule defaults.
- `generator/generate_art.py` — AIC search, scoring, image analysis, trimming, contain layout, line/Atkinson conversion, and 1bpp packing.
- `.github/workflows/daily-art.yml` — optional free daily static-feed build/deploy through GitHub Pages.
- `site/` — generated Pages site/feed output.

## Arduino setup

1. Install the current **Seeed_GFX** library.
2. Select the Seeed **XIAO ESP32-S3 Plus** board.
3. Enable **OPI PSRAM**.
4. Open `firmware/PaperGallery/PaperGallery.ino`.
5. Leave `driver.h` beside the sketch. It contains:

   ```cpp
   #define BOARD_SCREEN_COMBO 502
   #define USE_XIAO_EPAPER_DISPLAY_BOARD_EE04
   ```

6. Flash the sketch.

The firmware calls `WiFi.begin()` with no explicit credentials, so it reuses Wi-Fi credentials already stored in the ESP32 NVS. This is intentional for the existing Sidekick device. If the device has never been provisioned, provision Wi-Fi once before relying on the daily feed.

## Set up the art feed

The simplest V1 deployment is GitHub Pages:

1. Put this project in a GitHub repository.
2. In the repository, enable **Settings → Pages → Source: GitHub Actions**.
3. Run **Build Paper Gallery daily art pack** once from Actions.
4. Your feed will be at approximately:

   `https://YOUR_GITHUB_USERNAME.github.io/YOUR_REPOSITORY/feed`

5. Open `http://paper-gallery.local`, log in with `paper` / `2684`, paste that URL into **Art feed URL**, and save.
6. Press **Refresh feed**. The first artwork should appear.

You can also put the final URL directly in `config.h` before flashing.

## Feed format

Every daily pack contains:

- `index.txt` — pack date + slot count.
- `slot0.txt` … `slot7.txt` — plain-text metadata, artwork ID, and CRC32.
- `slot0.bin` … `slot7.bin` — exact 800×480, 1bpp, row-major, MSB-first frame buffers.
- `slot0.png` … `slot7.png` — browser previews.
- `index.json` — human/developer-friendly metadata.

The ESP32 validates the 48,000-byte frame and CRC32 before refreshing the physical panel.

## Generator image strategy

The generator first scores metadata, then scores the actual downloaded image for qualities that tend to work well on e-paper: light paper/background, useful edge density, contrast, and limited solid-black coverage. It chooses one of two renderers:

- **line** — conservative autolevel + local/adaptive threshold for drawings, plans, botanicals, etc.
- **atkinson** — Atkinson dithering for engravings/etchings/prints with meaningful tonal shading.

Artwork is **contained**, not aggressively cropped, and centered on an 800×480 white canvas after conservative scan-border trimming.

## Local generator test

```bash
python -m pip install -r generator/requirements.txt
python generator/generate_art.py --self-test
python generator/generate_art.py --output site/feed --site site --count 8
```

The full online generation step intentionally downloads museum images one at a time with a delay.

## V1 limitations / next work

- The firmware uses TLS without certificate verification for the public art feed to avoid CA-maintenance failures on the embedded frame. A pinned CA can be added later.
- No caption is drawn on the e-paper in V1; the default remains artwork-only.
- Favorites and Met/Cleveland sources are intentionally deferred until the AIC-only daily pipeline is stable.
- Wi-Fi provisioning UI is not reimplemented in this first refactor because the existing Sidekick device already has stored Wi-Fi credentials.
