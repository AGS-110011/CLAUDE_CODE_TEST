"""Paginate an Idealista areas search URL → list of listing URLs."""
from __future__ import annotations

import re
from datetime import datetime
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

from playwright.async_api import Page
from rich.console import Console

from ..scraper.browser import BrowserSession, _is_datadome_page
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

# Candidate selectors for the "next page" button
_NEXT_SELECTORS = [
    "a[aria-label='Siguiente']",
    "a.icon-arrow-right-after",
    ".pagination__next a",
    "li.next a",
    "a[rel='next']",
    "a[title='Siguiente']",
]


def _add_sort_param(url: str) -> str:
    """Append ordenado-por param to URL if not already present."""
    parts = urlsplit(url)
    query = parts.query
    if _SORT_PARAM not in query:
        query = (query + "&" + _SORT_PARAM) if query else _SORT_PARAM
    return urlunsplit((parts.scheme, parts.netloc, parts.path, query, ""))


def _parse_total_count(html: str) -> int | None:
    m = re.search(r"(\d[\d\.]*)\s+anuncios?", html, re.IGNORECASE)
    if m:
        return int(m.group(1).replace(".", ""))
    return None


def _save_debug_html(html: str, listing_type: str, page_num: int) -> None:
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    out = Path("debug") / f"{ts}_{listing_type}_page{page_num}.html"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(html, encoding="utf-8")
    console.print(f"  [yellow]Raw HTML saved → {out}  (open to inspect)[/yellow]")


async def _wait_for_listings(page: Page, timeout: int = 30_000) -> bool:
    """Wait for listing links to appear in the DOM. Returns True if found."""
    try:
        await page.wait_for_function(
            "() => document.querySelectorAll('a[href*=\"/inmueble/\"]').length > 0",
            timeout=timeout,
        )
        return True
    except Exception:
        return False


async def _get_listing_hrefs(page: Page) -> list[str]:
    """Query the live DOM for all /inmueble/ hrefs."""
    for selector in _LINK_SELECTORS:
        try:
            elements = await page.query_selector_all(selector)
            if elements:
                hrefs = []
                for el in elements:
                    href = await el.get_attribute("href")
                    if href and "/inmueble/" in href:
                        hrefs.append(href)
                if hrefs:
                    console.print(f"  [dim]Selector '{selector}' → {len(hrefs)} links[/dim]")
                    return hrefs
        except Exception:
            continue
    return []


async def _click_next_page(page: Page) -> bool:
    """Click the next-page button. Returns True if found and clicked."""
    for selector in _NEXT_SELECTORS:
        try:
            btn = await page.query_selector(selector)
            if btn:
                is_visible = await btn.is_visible()
                is_enabled = await btn.is_enabled()
                if is_visible and is_enabled:
                    await btn.click()
                    console.print(f"  [dim]Clicked next-page via '{selector}'[/dim]")
                    return True
        except Exception:
            continue
    return False


async def _handle_challenge_if_needed(
    page: Page, session: BrowserSession, page_num: int
) -> bool:
    """
    Check if current page is a DataDome challenge.
    In headful mode: pause and let user solve it.
    Returns True if a challenge was handled and we can continue.
    """
    content = await page.content()
    if not _is_datadome_page(content):
        return False

    if session.headful:
        console.print(
            f"[bold red]DataDome challenge on page {page_num}. "
            "Solve it in the browser, wait for listings to appear, "
            "then press ENTER.[/bold red]"
        )
        input("Press ENTER once listings are visible...")
        await session._save_session()
        return True
    else:
        console.print(
            f"  [red]DataDome challenge on page {page_num}. "
            "Rerun with --headful to solve interactively.[/red]"
        )
        return False


async def paginate_search(
    session: BrowserSession,
    base_url: str,
    listing_type: str,
    max_results: int,
    delay_min: float,
    delay_max: float,
) -> list[str]:
    """
    Paginate through a search URL using click-based navigation and return up
    to max_results deduplicated absolute listing URLs, ordered newest-first.
    """
    collected: list[str] = []
    seen: set[str] = set()
    base = "https://www.idealista.com"

    page: Page = await session.new_page()

    try:
        # Load page 1 with sort parameter
        start_url = _add_sort_param(base_url)
        console.print(f"  [cyan]Search page 1[/cyan] → {start_url[:90]}…")

        try:
            await session.navigate(page, start_url)
        except RuntimeError:
            raise
        except Exception as exc:
            console.print(f"  [red]Error loading page 1: {exc}[/red]")
            return []

        page_num = 1

        while len(collected) < max_results:
            # Wait for listings to appear
            found = await _wait_for_listings(page)

            if not found:
                # Check for silent DataDome challenge
                handled = await _handle_challenge_if_needed(page, session, page_num)
                if handled:
                    found = await _wait_for_listings(page)
                if not found:
                    html = await page.content()
                    console.print(
                        f"  [yellow]No listings found on page {page_num}; stopping.[/yellow]"
                    )
                    _save_debug_html(html, listing_type, page_num)
                    break

            # Print total count on page 1
            if page_num == 1:
                html = await page.content()
                total = _parse_total_count(html)
                if total:
                    console.print(
                        f"  [green]{listing_type}: {total} total listings in area.[/green]"
                    )

            # Collect hrefs from DOM
            hrefs = await _get_listing_hrefs(page)
            new = 0
            for href in hrefs:
                abs_url = base + href if href.startswith("/") else href
                if abs_url not in seen:
                    seen.add(abs_url)
                    collected.append(abs_url)
                    new += 1
                    if len(collected) >= max_results:
                        break

            console.print(
                f"  [green]Page {page_num}: +{new} new → {len(collected)} total[/green]"
            )

            if len(collected) >= max_results:
                break

            # Try clicking next page button
            await random_delay(delay_min, delay_max)
            console.print(f"  [cyan]Search page {page_num + 1}[/cyan] → clicking next…")

            clicked = await _click_next_page(page)
            if not clicked:
                console.print("  [green]No next-page button found; all pages collected.[/green]")
                break

            page_num += 1

            # Wait for new listings to load after click
            found = await _wait_for_listings(page, timeout=30_000)
            if not found:
                handled = await _handle_challenge_if_needed(page, session, page_num)
                if handled:
                    await _wait_for_listings(page)

    finally:
        await page.close()

    return collected[:max_results]
