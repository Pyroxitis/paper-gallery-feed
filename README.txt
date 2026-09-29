Paper Gallery Aerospace NTRS v2.2 — independently validated

Upload/replace in the root of paper-gallery-feed:
  generate_aerospace.py
  requirements.txt

No ESP32 firmware change is required.
No daily-art.yml change is required if it already runs:
  python generate_aerospace.py --self-test
  python generate_aerospace.py --output site/feed/aerospace --site site/aerospace --count-per-category 4

v2.2 fixes added after independent validation:
- Scanned/image-only NTRS PDFs now reserve render slots for pages distributed across the full document, rather than allowing weak/equal text scores to bias rendering toward early pages.
- An unexpected NTRS exception is isolated at the source-mixing layer; Commons continues building the Aerospace feed instead of the entire daily workflow failing.

Validation performed on the exact packaged generator:
- Python syntax and compileall checks.
- Built-in offline self-test.
- Independent 56-check harness covering:
  * rights allow/deny matrix
  * report/year/PDF-link parsing
  * POST search and GET fallback behavior
  * PDF size/magic/budget failure paths
  * distributed long-report page sampling
  * synthetic scanned multi-page PDF extraction
  * technical-vs-blank/prose/noise page scoring
  * 800x480 rendering and 48,000-byte packing
  * all-slot CRC and metadata consistency
  * contiguous category ranges
  * stale slot cleanup
  * deterministic output for a fixed selection/date
  * graceful Commons fallback on unexpected NTRS failure
- Installed dependency versions were checked against requirements specifiers.

independent_validation.py is included for audit/reference; it is not required by GitHub Actions.
