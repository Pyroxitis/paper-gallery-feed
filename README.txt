Paper Gallery Aerospace NTRS v2.1 — audited

Replace these files in the root of the paper-gallery-feed GitHub repository:
  generate_aerospace.py
  requirements.txt

No ESP32 firmware change is required.
No daily-art.yml change is required if it already runs:
  python generate_aerospace.py --self-test
  python generate_aerospace.py --output site/feed/aerospace --site site/aerospace --count-per-category 4

Audit fixes in v2.1:
- Corrected NTRS report-number parsing for list/dict/scalar API shapes.
- Hardened NTRS PDF-link selection and rejects non-PDF download entries.
- Hardened booleans and rights checks; still requires PUBLIC + DOCUMENT_AND_METADATA,
  explicit public-use copyright determination, no indicated third-party material, and no export restriction.
- Added daily NTRS download/attempt budgets to keep GitHub Actions bounded.
- Counts partially downloaded bytes against the daily budget.
- Validates PDF magic bytes before opening with PDFium.
- Scans a distributed sample across long reports rather than just the first pages.
- Fixed the fallback-page sampler so small sample sizes still reach late pages.
- Added render-pixel limits for giant PDF foldouts.
- Reworked page scoring so blank/title pages are strongly penalized and technical structure is rewarded.
- Prevents the same NTRS report from being reused across multiple categories in one daily pack.
- Randomizes which categories get the limited NTRS slots so later categories are not permanently starved.
- Fixed gallery source fallback for NTRS records without report numbers.
- Expanded offline self-tests for rights parsing, report-number shapes, distributed page selection,
  blank-page rejection, PDFium rendering, packing, and bit polarity.
