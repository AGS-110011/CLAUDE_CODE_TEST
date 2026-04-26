# idealista-extractor

Scrapes an Idealista **areas** search polygon (rent + sale) and writes a
fully-formatted Excel investment-analysis workbook with three sheets:
`INPUT_DATA`, `ANALYSIS` (dashboard), and `KEY_COMPARABLES`.

---

## Requirements

| Tool | Version |
|------|---------|
| Python | ≥ 3.11 |
| LibreOffice | Any recent (optional but recommended for dashboard recalculation) |

---

## Install

```bash
# 1. Clone / unzip the project
cd idealista-extractor

# 2. Create and activate a virtual environment
python -m venv .venv
# Windows
.venv\Scripts\activate
# macOS / Linux
source .venv/bin/activate

# 3. Install the package and its dependencies
pip install -e .

# 4. Download the Chromium browser for Playwright
playwright install chromium
```

---

## First-run: cookie priming (important)

Idealista uses DataDome bot-protection. On the very first run — or after a
session expires — you must prime the cookies interactively:

```bash
idealista-extractor \
  --sale-url "<SALE_URL>" \
  --rent-url "<RENT_URL>" \
  --headful
```

A visible Chrome window opens. If a DataDome challenge appears, solve it
(click-and-hold, Cloudflare checkbox, etc.). Once the search results load,
press **ENTER** in the terminal. The session is saved to
`.idealista_session.json` and reused on every subsequent run.

---

## Sample command (using the spec URLs)

```bash
idealista-extractor \
  --sale-url "https://www.idealista.com/areas/venta-viviendas/con-pisos,apartamentos/?shape=%28%28ugtuF%7EktUkb%40aD%7DPiG%7DFmb%40pG_%60%40kGd_%40EeIbEon%40xK%7DW%7EPa%40lSfBzVxC%7CPhVdGhd%40uCl%5BqFdXgKtHsTh%40%29%29" \
  --rent-url "https://www.idealista.com/areas/alquiler-viviendas/?shape=%28%28ugtuF%7EktUkb%40aD%7DPiG%7DFmb%40pG_%60%40kGd_%40EeIbEon%40xK%7DW%7EPa%40lSfBzVxC%7CPhVdGhd%40uCl%5BqFdXgKtHsTh%40%29%29" \
  --target-lat 40.42 \
  --target-lon -3.68 \
  --max-per-type 100 \
  --out comps.xlsx
```

The tool will:
1. Paginate both URLs (newest-first, capped at 100 each).
2. Extract all fields from every listing page.
3. Write `comps.xlsx` with the `INPUT_DATA`, `ANALYSIS`, and `KEY_COMPARABLES` sheets.
4. Run a LibreOffice headless recalculation pass so dashboard formulas show
   real numbers the moment you open the file.
5. Print a summary table with the headline KPIs.

---

## All CLI flags

| Flag | Default | Description |
|------|---------|-------------|
| `--sale-url` | — | Idealista sale areas URL (must contain `shape=`) |
| `--rent-url` | — | Idealista rent areas URL (must contain `shape=`) |
| `--target-lat` | — | Reference lat for distance column |
| `--target-lon` | — | Reference lon for distance column |
| `--out` | `./Idealista_Property_Analysis.xlsx` | Output path |
| `--max-per-type` | `100` | Cap per URL (1–200) |
| `--delay-min` | `4` | Min seconds between requests |
| `--delay-max` | `8` | Max seconds between requests |
| `--headful` | off | Show browser (first-run priming) |
| `--session-file` | `./.idealista_session.json` | Session state path |

---

## Running the tests

```bash
pip install -e ".[dev]"
pytest -v
```

Tests run entirely offline against the HTML fixtures in `tests/fixtures/`.

---

## Output workbook structure

### Sheet 1 — `INPUT_DATA`
One row per listing. Columns A–S:

| Col | Field | Type |
|-----|-------|------|
| A | ID | Integer |
| B | Type | Sale / Rent |
| C | Link | Hyperlink |
| D | Address | Text |
| E | Distance (m) | Number |
| F | Price (€) | Number |
| G | Size (m²) | Number |
| H | Price per m² | **Formula** |
| I | Bedrooms | Number |
| J | Bathrooms | Number |
| K | Floor | Text |
| L | Elevator | Yes / No |
| M | Condition | New / Renovated / To renovate |
| N | Year | Number |
| O | Terrace | Yes / No |
| P | Parking | Yes / No |
| Q | Listing Date | Date |
| R | Days on Market | **Formula** |
| S | Notes | Text |

Sale listings occupy IDs 1 … Ns; rent listings Ns+1 … Ns+Nr, both
ordered newest-first within each block.

### Sheet 2 — `ANALYSIS`
Auto-updating dashboard with:
- Sales price-per-m² statistics (avg, median, min, max, count)
- Rent statistics (avg monthly, avg per m², median per m², count)
- **Gross rental yield headline KPI** (annualised rent ÷ avg sale price)
- Market segmentation by condition
- Two charts: price/m² distribution (bar) and price vs size (scatter)

### Sheet 3 — `KEY_COMPARABLES`
Enter any ID from `INPUT_DATA` in column A; columns B–G auto-fill via
`INDEX/MATCH`. Column H is for manual comments.

---

## Troubleshooting

### DataDome challenge on every run
The session cookie has expired. Delete `.idealista_session.json` and rerun
with `--headful` to prime a fresh session.

### `soffice` not found — dashboard shows 0
Install LibreOffice and ensure `soffice` is on your PATH, or install it to
the default location. The workbook is still valid; open it in Excel and
press **Ctrl + Alt + F9** to force a full recalculation.

### `playwright install chromium` fails behind a proxy
Set the `HTTPS_PROXY` environment variable before running the install
command, or download the Chromium revision manually.

### Listings extracted as partial (`[partial extraction]` in Notes)
The page structure changed or a field was missing. The row is included with
whatever was captured. Check the `debug/` folder for screenshots of any
failed pages.

---

## Project layout

```
idealista_extractor/
  cli.py              CLI entry-point (Click)
  config.py           Config dataclass
  models.py           Pydantic Listing model
  scraper/
    browser.py        Stealth Playwright session, DataDome handling
    search.py         Paginate search URL → list of listing URLs
    listing.py        Parse one listing page → Listing dict
  excel/
    template.py       Build empty workbook skeleton
    writer.py         Fill data rows, run LibreOffice recalc, verify KPIs
  utils/
    geo.py            Haversine distance
    rate_limit.py     Random async delay
tests/
  test_listing_parser.py
  test_search_parser.py
  fixtures/           Offline HTML samples
```
