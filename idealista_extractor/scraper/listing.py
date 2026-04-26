"""Extract structured data from a single Idealista listing page."""
from __future__ import annotations

import json
import re
from datetime import date, timedelta
from typing import Any, Optional
from urllib.parse import urlparse

from playwright.async_api import Page
from rich.console import Console
from tenacity import retry, stop_after_attempt, wait_exponential

from ..models import Listing
from ..scraper.browser import BrowserSession
from ..utils.geo import haversine
from ..utils.rate_limit import random_delay

console = Console()

# ---------------------------------------------------------------------------
# Spanish → English condition mapping
# ---------------------------------------------------------------------------
_CONDITION_MAP: list[tuple[str, str]] = [
    ("obra nueva", "New"),
    ("a estrenar", "New"),
    ("buen estado", "Renovated"),
    ("reformado", "Renovated"),
    ("segunda mano/buen estado", "Renovated"),
    ("segunda mano / buen estado", "Renovated"),
    ("reformada", "Renovated"),
    ("a reformar", "To renovate"),
    ("para reformar", "To renovate"),
    ("necesita reforma", "To renovate"),
    ("en mal estado", "To renovate"),
]

_ENERGY_RE = re.compile(
    r"([A-G])\s*[–\-]?\s*(\d+(?:[.,]\d+)?)\s*kWh",
    re.IGNORECASE,
)


# ---------------------------------------------------------------------------
# JSON extraction helpers
# ---------------------------------------------------------------------------

def _extract_balanced_json(text: str, start: int) -> dict:
    """Extract a balanced JSON object starting at index start in text."""
    brace_start = text.find("{", start)
    if brace_start == -1:
        return {}
    depth = 0
    in_string = False
    escape_next = False
    for i, ch in enumerate(text[brace_start:], brace_start):
        if escape_next:
            escape_next = False
            continue
        if ch == "\\" and in_string:
            escape_next = True
            continue
        if ch == '"' and not escape_next:
            in_string = not in_string
        if not in_string:
            if ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    try:
                        return json.loads(text[brace_start : i + 1])
                    except json.JSONDecodeError:
                        return {}
    return {}


def _find_json_block(html: str, markers: list[str]) -> dict:
    """Try each marker; return first successfully parsed JSON object."""
    for marker in markers:
        idx = html.find(marker)
        if idx != -1:
            obj = _extract_balanced_json(html, idx)
            if obj:
                return obj
    return {}


def _deep_get(obj: Any, *keys: str) -> Any:
    """Traverse nested dict/list safely."""
    for k in keys:
        if isinstance(obj, dict):
            obj = obj.get(k)
        elif isinstance(obj, list) and k.isdigit():
            try:
                obj = obj[int(k)]
            except IndexError:
                return None
        else:
            return None
        if obj is None:
            return None
    return obj


# ---------------------------------------------------------------------------
# Field normalisers
# ---------------------------------------------------------------------------

def _clean_price(val: Any) -> Optional[int]:
    if val is None:
        return None
    s = str(val).replace(".", "").replace(",", "").replace("€", "").replace(" ", "")
    m = re.search(r"\d+", s)
    return int(m.group()) if m else None


def _clean_size(val: Any) -> Optional[float]:
    if val is None:
        return None
    s = str(val).replace(",", ".").replace("m²", "").replace("m2", "").strip()
    m = re.search(r"[\d.]+", s)
    return float(m.group()) if m else None


def _clean_int(val: Any) -> Optional[int]:
    if val is None:
        return None
    m = re.search(r"\d+", str(val))
    return int(m.group()) if m else None


def _clean_year(val: Any) -> Optional[int]:
    y = _clean_int(val)
    if y and 1800 < y <= 2030:
        return y
    return None


def _parse_date(val: Any) -> Optional[date]:
    if not val:
        return None
    s = str(val).strip()
    # ISO format
    m = re.match(r"(\d{4})-(\d{2})-(\d{2})", s)
    if m:
        try:
            return date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
        except ValueError:
            pass
    # Spanish "hace N días"
    m = re.search(r"hace\s+(\d+)\s+d", s, re.IGNORECASE)
    if m:
        return date.today() - timedelta(days=int(m.group(1)))
    # "hoy"
    if "hoy" in s.lower():
        return date.today()
    # "ayer"
    if "ayer" in s.lower():
        return date.today() - timedelta(days=1)
    return None


def _map_condition(raw: Any) -> Optional[str]:
    if not raw:
        return None
    s = str(raw).lower().strip()
    for spanish, english in _CONDITION_MAP:
        if spanish in s:
            return english
    return None


def _yes_no(val: Any) -> Optional[str]:
    if val is None:
        return None
    if isinstance(val, bool):
        return "Yes" if val else "No"
    s = str(val).lower()
    if s in ("true", "1", "yes", "sí", "si"):
        return "Yes"
    if s in ("false", "0", "no"):
        return "No"
    return None


# ---------------------------------------------------------------------------
# Feature bullet parser
# ---------------------------------------------------------------------------

def _parse_features(
    features: list[str],
    notes_parts: list[str],
) -> dict:
    """
    Parse a list of Spanish feature strings and return a dict of extracted fields.
    Side-effect: appends to notes_parts.
    """
    result: dict[str, Any] = {}
    fl = [f.lower().strip() for f in features]
    joined = " | ".join(fl)

    # Elevator
    if any("ascensor" in f and "sin " not in f for f in fl):
        result["elevator"] = "Yes"
    elif any("sin ascensor" in f for f in fl):
        result["elevator"] = "No"

    # Parking
    if any(p in joined for p in ("plaza de garaje incluida", "con plaza de garaje", "garaje incluido")):
        result["parking"] = "Yes"
    elif any(p in joined for p in ("plaza de garaje opcional", "garaje opcional", "plaza opcional")):
        result["parking"] = "No"
        # Extract optional price if present
        m = re.search(r"opcional[^\d]*(\d[\d\.,]*)\s*€", joined)
        if m:
            price_str = m.group(1).replace(".", "").replace(",", "")
            notes_parts.append(f"optional parking +€{price_str}")
        else:
            notes_parts.append("optional parking")

    # Terrace
    if any("terraza" in f for f in fl):
        result["terrace"] = "Yes"
    elif any("balc" in f for f in fl):
        result["terrace"] = "No"
        notes_parts.append("balcony only")

    # Condition (from feature bullets)
    for f in fl:
        cond = _map_condition(f)
        if cond:
            result["condition"] = cond
            break

    # Energy certificate
    for f in features:
        em = _ENERGY_RE.search(f)
        if em:
            rating = em.group(1).upper()
            kwh = em.group(2).replace(",", ".")
            notes_parts.insert(0, f"{rating} {kwh} kWh/m²")
            break

    # Orientation
    orient_terms = ["exterior", "interior", "sur", "norte", "este", "oeste"]
    for f in fl:
        if any(t in f for t in orient_terms):
            val = f.replace("orientación", "").replace("orientacion", "").strip(" :,")
            if val:
                notes_parts.append(val)
            break

    # AC
    if any("aire acondicionado" in f for f in fl):
        notes_parts.append("aire acondicionado")

    # Heating
    for f in fl:
        if "calefacción" in f or "calefaccion" in f:
            notes_parts.append(f.strip())
            break

    # Built-in wardrobes
    if any("armarios empotrados" in f for f in fl):
        notes_parts.append("armarios empotrados")

    return result


# ---------------------------------------------------------------------------
# Primary extraction: embedded JSON
# ---------------------------------------------------------------------------

def _extract_from_json(html: str) -> dict:
    """Try multiple JSON embedding patterns; return best dict found."""
    candidates = [
        "utag_data =",
        "utag_data=",
        '"adDetail"',
        '"adCommons"',
        "window.__INITIAL_STATE__",
        "window.reactData",
        "__NEXT_DATA__",
    ]
    data = _find_json_block(html, candidates)

    # __NEXT_DATA__ wraps inside props.pageProps
    if "props" in data:
        inner = _deep_get(data, "props", "pageProps") or {}
        if inner:
            data = inner

    return data


# ---------------------------------------------------------------------------
# Fallback: CSS / regex scraping
# ---------------------------------------------------------------------------

def _scrape_price_from_html(html: str) -> Optional[int]:
    patterns = [
        r'class="[^"]*info-data-price[^"]*"[^>]*>.*?<span[^>]*>([\d\.,]+)',
        r'class="[^"]*txt-bold[^"]*"[^>]*>([\d\.,]+)\s*€',
        r'"price"\s*:\s*"?([\d]+)"?',
    ]
    for pat in patterns:
        m = re.search(pat, html, re.DOTALL | re.IGNORECASE)
        if m:
            v = _clean_price(m.group(1))
            if v:
                return v
    return None


def _scrape_features_from_html(html: str) -> dict:
    """Extract size, rooms, bathrooms from .info-features spans."""
    result: dict = {}
    # Size
    m = re.search(r"([\d\.,]+)\s*m²", html, re.IGNORECASE)
    if m:
        result["size"] = _clean_size(m.group(1))
    # Rooms
    m = re.search(r"(\d+)\s+hab\.", html, re.IGNORECASE)
    if m:
        result["rooms"] = int(m.group(1))
    # Bathrooms
    m = re.search(r"(\d+)\s+ba[ñn]", html, re.IGNORECASE)
    if m:
        result["bathrooms"] = int(m.group(1))
    # Floor
    m = re.search(
        r"(Bajo|Semisótano|Entresuelo|Principal|\d+ª?\s*planta|Ático|Último piso)",
        html,
        re.IGNORECASE,
    )
    if m:
        result["floor"] = m.group(1).strip()
    return result


def _scrape_address_from_html(html: str) -> str:
    m = re.search(
        r'class="[^"]*main-info__title[^"]*"[^>]*>(.*?)</span',
        html,
        re.DOTALL | re.IGNORECASE,
    )
    if m:
        return re.sub(r"<[^>]+>", "", m.group(1)).strip()
    m = re.search(
        r'<h1[^>]*class="[^"]*title[^"]*"[^>]*>(.*?)</h1>',
        html,
        re.DOTALL | re.IGNORECASE,
    )
    if m:
        return re.sub(r"<[^>]+>", "", m.group(1)).strip()
    return "Unknown address"


def _scrape_feature_bullets(html: str) -> list[str]:
    bullets = re.findall(
        r'<li[^>]*class="[^"]*details-property[^"]*"[^>]*>(.*?)</li>',
        html,
        re.DOTALL | re.IGNORECASE,
    )
    if not bullets:
        bullets = re.findall(
            r'<span[^>]*class="[^"]*tag[^"]*"[^>]*>(.*?)</span>',
            html,
            re.DOTALL | re.IGNORECASE,
        )
    return [re.sub(r"<[^>]+>", "", b).strip() for b in bullets if b.strip()]


def _scrape_listing_date(html: str) -> Optional[date]:
    m = re.search(
        r'(?:Publicado|Actualizado)[^\d]*(\d{1,2})\s+de\s+(\w+)\s+de\s+(\d{4})',
        html,
        re.IGNORECASE,
    )
    if m:
        months = {
            "enero": 1, "febrero": 2, "marzo": 3, "abril": 4,
            "mayo": 5, "junio": 6, "julio": 7, "agosto": 8,
            "septiembre": 9, "octubre": 10, "noviembre": 11, "diciembre": 12,
        }
        month_num = months.get(m.group(2).lower())
        if month_num:
            try:
                return date(int(m.group(3)), month_num, int(m.group(1)))
            except ValueError:
                pass
    m2 = re.search(r"hace\s+(\d+)\s+d[íi]as?", html, re.IGNORECASE)
    if m2:
        return date.today() - timedelta(days=int(m2.group(1)))
    if re.search(r"\bhoy\b", html, re.IGNORECASE):
        return date.today()
    if re.search(r"\bayer\b", html, re.IGNORECASE):
        return date.today() - timedelta(days=1)
    return None


# ---------------------------------------------------------------------------
# Source ID from URL
# ---------------------------------------------------------------------------

def _source_id_from_url(url: str) -> str:
    parts = urlparse(url).path.strip("/").split("/")
    for part in reversed(parts):
        if part.isdigit():
            return part
    return parts[-1] if parts else "0"


# ---------------------------------------------------------------------------
# Main public function
# ---------------------------------------------------------------------------

async def extract_listing(
    session: BrowserSession,
    url: str,
    listing_type: str,
    delay_min: float,
    delay_max: float,
    target_lat: Optional[float] = None,
    target_lon: Optional[float] = None,
) -> dict:
    """
    Navigate to listing page and extract all fields.
    Returns a dict suitable for Listing(**dict) — id is set later by writer.
    Never raises; partial rows get '[partial extraction]' note.
    """
    notes_parts: list[str] = []
    partial = False
    source_id = _source_id_from_url(url)

    page: Page = await session.new_page()
    try:
        html = await session.navigate(page, url, wait_selector=".info-data-price", timeout=25_000)
    except Exception as exc:
        console.print(f"  [red]Failed to load {url}: {exc}[/red]")
        await page.close()
        return _partial_row(source_id, url, listing_type, str(exc))
    finally:
        await page.close()

    await random_delay(delay_min, delay_max)

    # --- JSON extraction ---
    json_data = _extract_from_json(html)

    price: Optional[int] = None
    size: Optional[float] = None
    bedrooms: Optional[int] = None
    bathrooms: Optional[int] = None
    lat: Optional[float] = None
    lon: Optional[float] = None
    address = ""
    floor: Optional[str] = None
    elevator: Optional[str] = None
    condition: Optional[str] = None
    year: Optional[int] = None
    terrace: Optional[str] = None
    parking: Optional[str] = None
    listing_date: Optional[date] = None
    feature_bullets: list[str] = []

    if json_data:
        # Try flat utag_data style keys
        price = _clean_price(
            json_data.get("price")
            or json_data.get("ad_price")
            or _deep_get(json_data, "adDetail", "price")
        )
        size = _clean_size(
            json_data.get("surface")
            or json_data.get("size")
            or json_data.get("constructedArea")
            or _deep_get(json_data, "adDetail", "surface")
        )
        bedrooms = _clean_int(
            json_data.get("rooms")
            or json_data.get("bedrooms")
            or _deep_get(json_data, "adDetail", "rooms")
        )
        bathrooms = _clean_int(
            json_data.get("bathrooms")
            or _deep_get(json_data, "adDetail", "bathrooms")
        )
        try:
            lat = float(json_data.get("latitude") or json_data.get("lat") or 0) or None
            lon = float(json_data.get("longitude") or json_data.get("lng") or 0) or None
        except (TypeError, ValueError):
            lat = lon = None
        address = (
            str(json_data.get("address") or json_data.get("ad_address") or "").strip()
            or _scrape_address_from_html(html)
        )
        floor = (
            str(json_data.get("floor") or "").strip() or None
        )
        elevator_raw = (
            json_data.get("hasLift")
            or json_data.get("lift")
            or json_data.get("has_lift")
        )
        elevator = _yes_no(elevator_raw)

        condition_raw = (
            json_data.get("conservationState")
            or json_data.get("condition")
            or json_data.get("conservation_state")
        )
        condition = _map_condition(condition_raw)

        year = _clean_year(
            json_data.get("constructedYear")
            or json_data.get("yearConstructed")
            or json_data.get("year_built")
        )

        terrace_raw = json_data.get("hasTerrace") or json_data.get("terrace")
        terrace = _yes_no(terrace_raw)

        parking_raw = json_data.get("hasParking") or json_data.get("parking")
        if parking_raw is not None:
            parking = _yes_no(parking_raw)
            if parking == "No":
                parking_price = json_data.get("parkingPrice") or json_data.get("parking_price")
                if parking_price:
                    notes_parts.append(f"optional parking +€{parking_price}")

        listing_date = _parse_date(
            json_data.get("publishDate")
            or json_data.get("publishedDate")
            or json_data.get("date")
        )

        features_raw = json_data.get("features") or json_data.get("tags") or []
        if isinstance(features_raw, list):
            feature_bullets = [str(f) for f in features_raw]
        elif isinstance(features_raw, dict):
            feature_bullets = list(features_raw.values())

        # Energy certificate
        ec = json_data.get("energyCertificate") or json_data.get("energy_certificate") or ""
        if ec:
            em = _ENERGY_RE.search(str(ec))
            if em:
                notes_parts.insert(0, f"{em.group(1).upper()} {em.group(2)} kWh/m²")

    # --- Fallback: CSS scraping ---
    if not price:
        price = _scrape_price_from_html(html)

    if not size or not bedrooms:
        fb = _scrape_features_from_html(html)
        size = size or fb.get("size")
        bedrooms = bedrooms or fb.get("rooms")
        bathrooms = bathrooms or fb.get("bathrooms")
        floor = floor or fb.get("floor")

    if not address:
        address = _scrape_address_from_html(html)

    if not feature_bullets:
        feature_bullets = _scrape_feature_bullets(html)

    if not listing_date:
        listing_date = _scrape_listing_date(html)

    # --- Feature bullet parsing (always run on bullets) ---
    if feature_bullets:
        fdata = _parse_features(feature_bullets, notes_parts)
        elevator = elevator or fdata.get("elevator")
        parking = parking or fdata.get("parking")
        terrace = terrace or fdata.get("terrace")
        condition = condition or fdata.get("condition")

    # --- Distance ---
    distance_m: Optional[int] = None
    if target_lat is not None and target_lon is not None and lat and lon:
        distance_m = haversine(target_lat, target_lon, lat, lon)

    # --- Validation ---
    if not price or not size:
        partial = True
        notes_parts.append("[partial extraction]")
        price = price or 0
        size = size or 0.0

    notes = ", ".join(p for p in notes_parts if p)[:200]

    return dict(
        id=0,  # assigned by writer
        source_id=source_id,
        type=listing_type,
        link=url,
        address=address or "Unknown address",
        distance_m=distance_m,
        price_eur=int(price),
        size_m2=float(size),
        bedrooms=bedrooms,
        bathrooms=bathrooms,
        floor=floor,
        elevator=elevator,
        condition=condition,
        year=year,
        terrace=terrace,
        parking=parking,
        listing_date=listing_date,
        notes=notes,
    )


def _partial_row(source_id: str, url: str, listing_type: str, reason: str) -> dict:
    return dict(
        id=0,
        source_id=source_id,
        type=listing_type,
        link=url,
        address="Unknown address",
        distance_m=None,
        price_eur=0,
        size_m2=0.0,
        bedrooms=None,
        bathrooms=None,
        floor=None,
        elevator=None,
        condition=None,
        year=None,
        terrace=None,
        parking=None,
        listing_date=None,
        notes=f"[partial extraction] {reason}"[:200],
    )
