"""Paginate an Idealista areas search URL → list of listing URLs."""
from __future__ import annotations

import re
from datetime import datetime
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

from playwright.async_api import Page
from rich.console import Console

from ..scraper.browser import BrowserSession
from ..utils.rate_limit import random_delay

console = Console()

_SORT_PARAM = "ordenado-por=fecha-publicacion-desc"

# Candidate selectors for listing links — tried in order until one works
_LINK_SELECTORS = [
    "a.item-link[href*='/inmueble/']",
    "a[href*='/inmueble/']",
    "article.item a[href]",
    ".item-info-container a[href]",
]


def _build_page_url(base_url: str, page: int) -> str:
    """
    Construct paginated URL preserving shape and sort parameters.

    Page 1: .../areas/venta-viviendas/con-pisos,apartamentos/?shape=...&ordenado-por=...
    Page N: .../areas/venta-viviendas/con-pisos,apartamentos/pagina-N.htm?shape=...&ordenado-por=...
    """
    parts = urlsplit(base_url)
    path = parts.path.rstrip("/")

    # Strip any existing /pagina-N.htm segment
    path = re.sub(r"/pagina-\d+\.htm$", "", path)

    if page == 1:
        new_path = path + "/"
    else:
        new_path = f"{path}/pagina-{page}.htm"

    query = parts.query
    if _SORT_PARAM not in query:
        query = (query + "&" + _SORT_PARAM) if query else _SORT_PARAM

    return urlunsplit((parts.scheme, parts.netloc, new_path, query, ""))


def _parse_total_count(html: str) -> int | None:
    """Extract total listing count from h1 or .h1-simulated text."""
    m = re.search(r"(\d[\d\.]*)\s+anuncios?", html, re.IGNORECASE)
    if m:
        return int(m.group(1).replace(".", ""))
    return None


def _extract_listing_urls_from_html(html: str) -> list[str]:
    """Fallback: regex scan of raw HTML for /inmueble/NNNN/ hrefs."""
    urls = re.findall(r'href="(/inmueble/\d+/[^"]*)"', html)
    if not urls:
        urls = re.findall(r"href='(/inmueble/\d+/[^']*)'", html)
    if not urls:
        urls = re.findall(r'(/inmueble/\d+/)', html)
    return list(dict.fromkeys(urls))  # deduplicate preserving order


async def _extract_listing_urls_from_dom(page: Page) -> list[str]:
    """
    Primary: query the live DOM for listing hrefs using multiple selector
    candidates. Returns absolute or root-relative paths.
    """
    for selector in _LINK_SELECTORS:
        try:
            elements = await page.query_selector_all(selector)
            if elements:
                hrefs: list[str] = []
                for el in elements:
                    href = await el.get_attribute("href")
                    if href and "/inmueble/" in href:
                        hrefs.append(href)
                if hrefs:
                    console.print(
                        f"  [dim]DOM selector '{selector}' → {len(hrefs)} links[/dim]"
                    )
                    return hrefs
        except Exception:
            continue
    return []


def _save_debug_html(html: str, listing_type: str, page_num: int) -> None:
    """Save raw HTML to debug/ for inspection."""
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    out = Path("debug") / f"{ts}_{listing_type}_page{page_num}.html"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(html, encoding="utf-8")
    console.print(f"  [yellow]Raw HTML saved → {out}  (open to inspect)[/yellow]")


async def paginate_search(
    session: BrowserSession,
    base_url: str,
    listing_type: str,
    max_results: int,
    delay_min: float,
    delay_max: float,
) -> list[str]:
    """
    Paginate through a search URL and return up to max_results deduplicated
    absolute listing URLs, ordered newest-first.
    """
    collected: list[str] = []
    seen: set[str] = set()
    page_num = 1
    base = "https://www.idealista.com"

    page: Page = await session.new_page()

    try:
        while len(collected) < max_results:
            url = _build_page_url(base_url, page_num)
            console.print(f"  [cyan]Search page {page_num}[/cyan] → {url[:90]}…")

            try:
                html = await session.navigate(page, url)
            except RuntimeError:
                raise
            except Exception as exc:
                console.print(f"  [red]Error on search page {page_num}: {exc}[/red]")
                break

            if page_num == 1:
                total = _parse_total_count(html)
                if total is not None:
                    console.print(
                        f"  [green]{listing_type}: found {total} total listings.[/green]"
                    )

            # Try DOM query first (works on JS-rendered pages), fall back to HTML regex
            hrefs = await _extract_listing_urls_from_dom(page)
            if not hrefs:
                console.print("  [dim]DOM query found nothing; falling back to HTML regex[/dim]")
                hrefs = _extract_listing_urls_from_html(html)

            if not hrefs:
                console.print(
                    f"  [yellow]No listings found on page {page_num}; stopping.[/yellow]"
                )
                _save_debug_html(html, listing_type, page_num)
                break

            for href in hrefs:
                abs_url = base + href if href.startswith("/") else href
                if abs_url not in seen:
                    seen.add(abs_url)
                    collected.append(abs_url)
                    if len(collected) >= max_results:
                        break

            console.print(f"  [green]Collected {len(collected)} {listing_type} so far.[/green]")

            if len(collected) >= max_results:
                break

            page_num += 1
            await random_delay(delay_min, delay_max)
    finally:
        await page.close()

    return collected[:max_results]
