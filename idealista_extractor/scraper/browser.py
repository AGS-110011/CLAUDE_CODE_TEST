"""Stealth Playwright browser context with cookie/session persistence."""
from __future__ import annotations

import asyncio
import json
import os
from datetime import datetime
from pathlib import Path
from typing import AsyncGenerator

from playwright.async_api import (
    Browser,
    BrowserContext,
    Page,
    Playwright,
    async_playwright,
)
from rich.console import Console

console = Console()

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)

_STEALTH_JS = """
() => {
    // Mask webdriver flag
    Object.defineProperty(navigator, 'webdriver', { get: () => false });

    // Realistic plugins array
    Object.defineProperty(navigator, 'plugins', {
        get: () => [1, 2, 3, 4, 5],
    });

    // Realistic languages
    Object.defineProperty(navigator, 'languages', {
        get: () => ['es-ES', 'es', 'en-US', 'en'],
    });

    // Chrome runtime stub
    if (!window.chrome) {
        window.chrome = { runtime: {} };
    }

    // Permissions stub
    const originalQuery = window.navigator.permissions.query;
    window.navigator.permissions.query = (parameters) => (
        parameters.name === 'notifications'
            ? Promise.resolve({ state: Notification.permission })
            : originalQuery(parameters)
    );
}
"""


def _is_datadome_page(content: str) -> bool:
    # Only match the actual block/challenge page, NOT normal pages that
    # reference datadome in their JS (which every Idealista page does).
    markers = [
        "se ha detectado un uso indebido",
        "el acceso se ha bloqueado",
        "interstitial-datadome",
        "dd_referrer",
        "Please enable JS and disable any ad blocker",
    ]
    lower = content.lower()
    return any(m.lower() in lower for m in markers)


class BrowserSession:
    """Manages a single long-lived Playwright browser context."""

    def __init__(
        self,
        session_file: str = "./.idealista_session.json",
        headful: bool = False,
        debug_dir: str = "./debug",
    ) -> None:
        self.session_file = Path(session_file)
        self.headful = headful
        self.debug_dir = Path(debug_dir)
        self._playwright: Playwright | None = None
        self._browser: Browser | None = None
        self._context: BrowserContext | None = None

    async def __aenter__(self) -> BrowserSession:
        self._playwright = await async_playwright().start()

        launch_kwargs: dict = {
            "headless": not self.headful,
            "args": [
                "--disable-blink-features=AutomationControlled",
                "--no-sandbox",
                "--disable-setuid-sandbox",
            ],
        }

        self._browser = await self._playwright.chromium.launch(**launch_kwargs)

        context_kwargs: dict = {
            "user_agent": USER_AGENT,
            "locale": "es-ES",
            "timezone_id": "Europe/Madrid",
            "viewport": {"width": 1366, "height": 768},
            "extra_http_headers": {
                "Accept-Language": "es-ES,es;q=0.9,en;q=0.8",
                "Accept": (
                    "text/html,application/xhtml+xml,application/xml;"
                    "q=0.9,image/avif,image/webp,*/*;q=0.8"
                ),
            },
        }

        # Reload saved session state if available
        if self.session_file.exists():
            context_kwargs["storage_state"] = str(self.session_file)
            console.print(f"[green]Loaded session from {self.session_file}[/green]")
        else:
            console.print("[yellow]No saved session found; starting fresh.[/yellow]")

        self._context = await self._browser.new_context(**context_kwargs)
        await self._context.add_init_script(_STEALTH_JS)

        return self

    async def __aexit__(self, *_: object) -> None:
        if self._context:
            await self._save_session()
            await self._context.close()
        if self._browser:
            await self._browser.close()
        if self._playwright:
            await self._playwright.stop()

    async def _save_session(self) -> None:
        if self._context:
            self.session_file.parent.mkdir(parents=True, exist_ok=True)
            await self._context.storage_state(path=str(self.session_file))
            console.print(f"[green]Session saved → {self.session_file}[/green]")

    async def new_page(self) -> Page:
        assert self._context is not None
        page = await self._context.new_page()
        return page

    async def navigate(
        self,
        page: Page,
        url: str,
        wait_selector: str | None = None,
        timeout: int = 60_000,
    ) -> str:
        """Navigate to URL; return page HTML. Raises on DataDome block."""
        await page.goto(url, wait_until="domcontentloaded", timeout=timeout)

        # Give JS-heavy pages extra time to render
        try:
            await page.wait_for_load_state("networkidle", timeout=15_000)
        except Exception:
            pass  # networkidle timeout is non-fatal; continue with whatever loaded

        if wait_selector:
            try:
                await page.wait_for_selector(wait_selector, timeout=timeout)
            except Exception:
                pass

        content = await page.content()

        if _is_datadome_page(content):
            await self._save_debug_snapshot(page, url)
            if self.headful:
                console.print(
                    "[bold red]DataDome / block page detected. "
                    "Wait for the search results to fully load in the browser, "
                    "then press ENTER.[/bold red]"
                )
                input("Press ENTER once the listings are visible in the browser...")
                # Wait for network to settle after manual interaction
                try:
                    await page.wait_for_load_state("networkidle", timeout=20_000)
                except Exception:
                    pass
                await self._save_session()
                content = await page.content()
            else:
                raise RuntimeError(
                    "DataDome challenge detected. "
                    f"Debug files saved to {self.debug_dir}. "
                    "Rerun with --headful to solve the challenge interactively."
                )

        return content

    async def _save_debug_snapshot(self, page: Page, url: str) -> None:
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        out = self.debug_dir / ts
        out.mkdir(parents=True, exist_ok=True)
        await page.screenshot(path=str(out / "screenshot.png"), full_page=True)
        html = await page.content()
        (out / "page.html").write_text(html, encoding="utf-8")
        console.print(f"[yellow]Debug snapshot saved → {out}[/yellow]")
