"""Paginate an Idealista areas search URL → list of listing URLs."""
from __future__ import annotations

import re
from urllib.parse import urlsplit, urlunsplit

from playwright.async_api import Page
from rich.console import Console
from tenacity import retry, stop_after_attempt, wait_exponential

from ..scraper.browser import BrowserSession
from ..utils.rate_limit import random_delay

console = Console()

_SORT_PARAM = "ordenado-por=fecha-publicacion-desc"


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


def _extract_listing_urls(html: str) -> list[str]:
    """Extract all /inmueble/NNNN/ href values from a search results page."""
    # Try double-quoted hrefs first, then single-quoted, then unquoted
    urls = re.findall(r'href="(/inmueble/\d+/[^"]*)"', html)
    if not urls:
        urls = re.findall(r"href='(/inmueble/\d+/[^']*)'", html)
    if not urls:
        urls = re.findall(r'href=(/inmueble/\d+/\S+)', html)
    return urls


def _save_debug_html(html: str, listing_type: str, page_num: int) -> None:
    """Save raw HTML to debug/ so we can inspect what the page returned."""
    from datetime import datetime
    from pathlib import Path
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    out = Path("debug") / f"{ts}_{listing_type}_page{page_num}.html"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(html, encoding="utf-8")
    console.print(f"  [yellow]Raw HTML saved → {out}  (open to inspect)[/yellow]")


def _has_next_page(html: str) -> bool:
    """Return True if a 'next page' link is present and not disabled."""
    return bool(re.search(r'class="[^"]*next[^"]*"', html, re.IGNORECASE)) and not bool(
        re.search(r'class="[^"]*next[^"]*\bdisabled\b', html, re.IGNORECASE)
    )


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
                # Wait for listing cards to appear in the DOM before reading HTML
                html = await session.navigate(
                    page, url, wait_selector="article.item"
                )
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

            hrefs = _extract_listing_urls(html)
            if not hrefs:
                console.print(f"  [yellow]No listings found on page {page_num}; stopping.[/yellow]")
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
