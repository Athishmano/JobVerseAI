"""
┌─ FILE: job_bot/core/browser.py
├─ PURPOSE: Singleton Playwright browser context manager shared across all scrapers
│           in a single run. Handles launch, stealth, random delays, and retry logic.
├─ USED BY: scrapers/base.py (BaseScraper.run creates pages via BrowserManager.new_page),
│           services/pipeline.py (creates and closes the BrowserManager for the run)
├─ DATA FLOW: Settings → BrowserManager.start() → persistent BrowserContext
│             → BaseScraper.run() → new_page() → Page (with stealth applied)
├─ DESIGN DECISIONS: launch_persistent_context (not launch + new_context) so the
│                    session cookies in browser_data/ survive across bot runs and
│                    manual logins only happen once per portal.
│                    with_retry is a standalone decorator so any async function in any
│                    module can import and use it — not coupled to BrowserManager.
└─ PATTERNS: Async context manager, singleton, retry with exponential backoff, stealth
"""

from __future__ import annotations

import asyncio
import functools
import logging
import random
from pathlib import Path
from typing import Any, Callable, TypeVar

from playwright.async_api import (
    BrowserContext,
    Page,
    Playwright,
    async_playwright,
)

from job_bot.core.config import Settings
from job_bot.core.logger import log_error, log_warning

logger = logging.getLogger(__name__)

# ── Project root (browser_data/ lives here) ────────────────────────────────────
_ROOT = Path(__file__).resolve().parents[2]
_BROWSER_DATA_DIR = _ROOT / "browser_data"

# ── Stealth ────────────────────────────────────────────────────────────────────
_USER_AGENTS: list[str] = [
    # Chrome 125 on Windows 10
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
    # Chrome 124 on Windows 10
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    # Chrome 125 on macOS Ventura
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
    # Chrome 124 on Linux
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
]

_VIEWPORTS: list[dict[str, int]] = [
    {"width": 1920, "height": 1080},
    {"width": 1366, "height": 768},
    {"width": 1440, "height": 900},
    {"width": 1332, "height": 942},
    {"width": 1280, "height": 800},
]

# Injected into every new page before any site JavaScript runs
_STEALTH_INIT_SCRIPT = """
(function () {
    // Remove the webdriver flag that automation-detection scripts look for
    Object.defineProperty(navigator, 'webdriver', {
        get: () => undefined,
        configurable: true,
    });

    // Make plugins non-empty (headless Chrome has 0 by default)
    Object.defineProperty(navigator, 'plugins', {
        get: () => [
            { name: 'Chrome PDF Plugin' },
            { name: 'Chrome PDF Viewer' },
            { name: 'Native Client' },
        ],
        configurable: true,
    });

    // Realistic language list
    Object.defineProperty(navigator, 'languages', {
        get: () => ['en-US', 'en'],
        configurable: true,
    });

    // Stub chrome runtime so fingerprinting scripts don't flag its absence
    if (!window.chrome) {
        window.chrome = { runtime: {}, loadTimes: function(){}, csi: function(){} };
    }

    // Mask permission query — headless Chrome returns 'denied' for notifications
    const origQuery = window.navigator.permissions.query;
    window.navigator.permissions.query = (parameters) =>
        parameters.name === 'notifications'
            ? Promise.resolve({ state: Notification.permission })
            : origQuery(parameters);
})();
"""


# ── Retry decorator ────────────────────────────────────────────────────────────

F = TypeVar("F", bound=Callable[..., Any])


def with_retry(
    max_attempts: int = 3,
    base_delay: float = 2.0,
    exceptions: tuple[type[Exception], ...] = (Exception,),
) -> Callable[[F], F]:
    """
    Async retry decorator with exponential back-off and jitter.

    Logs each failed attempt at WARNING level with the format:
        [browser.py:<fn>] Attempt N/max failed: <exc> — retrying in Xs

    On the final attempt, the exception is re-raised so the caller can handle it.

    Args:
        max_attempts: Total number of tries (including the first attempt).
        base_delay:   Initial retry delay in seconds; doubles each attempt (+ jitter).
        exceptions:   Exception types that trigger a retry; others propagate immediately.

    Usage:
        @with_retry(max_attempts=3, base_delay=1.0)
        async def fetch_page(page: Page, url: str) -> str:
            await page.goto(url, timeout=30_000)
            return await page.content()
    """
    def decorator(func: F) -> F:
        @functools.wraps(func)
        async def wrapper(*args: Any, **kwargs: Any) -> Any:
            for attempt in range(1, max_attempts + 1):
                try:
                    return await func(*args, **kwargs)
                except exceptions as exc:
                    if attempt == max_attempts:
                        raise  # exhausted all attempts — let caller decide
                    delay = base_delay * (2 ** (attempt - 1)) + random.uniform(0.0, 0.5)
                    log_warning(
                        logger,
                        "browser.py",
                        func.__name__,
                        f"Attempt {attempt}/{max_attempts} failed: {exc}",
                        f"retrying in {delay:.1f}s",
                    )
                    await asyncio.sleep(delay)
            return None  # unreachable — satisfies type checker
        return wrapper  # type: ignore[return-value]
    return decorator


# ── BrowserManager ─────────────────────────────────────────────────────────────

class BrowserManager:
    """
    Manages the single Playwright BrowserContext shared across all scrapers in a run.

    Uses ``launch_persistent_context`` so session cookies written to browser_data/
    survive between bot runs — the user only needs to log in manually once per portal.

    Usage (async context manager):
        async with BrowserManager(settings) as browser:
            page = await browser.new_page()
            ...
    """

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self._playwright: Playwright | None = None
        self._context: BrowserContext | None = None
        self._user_agent: str = random.choice(_USER_AGENTS)
        self._viewport: dict[str, int] = random.choice(_VIEWPORTS)

    # ── Lifecycle ──────────────────────────────────────────────────────────────

    async def start(self) -> None:
        """
        Launch Playwright and open the persistent Chromium context.

        Logs:
            🔴 Browser launched — viewport WxH, user-data-dir=...
            📌 IMPORTANT: A browser window is now open. ...
        """
        _BROWSER_DATA_DIR.mkdir(parents=True, exist_ok=True)

        try:
            self._playwright = await async_playwright().start()
            self._context = await self._playwright.chromium.launch_persistent_context(
                user_data_dir=str(_BROWSER_DATA_DIR),
                headless=self.settings.headless_mode,
                user_agent=self._user_agent,
                viewport=self._viewport,  # type: ignore[arg-type]
                locale="en-US",
                timezone_id="Asia/Kolkata",
                args=[
                    "--disable-blink-features=AutomationControlled",
                    "--no-first-run",
                    "--no-default-browser-check",
                ],
                ignore_default_args=["--enable-automation"],
            )
        except Exception as exc:
            log_error(
                logger, "browser.py", "start",
                f"Failed to launch Playwright browser: {exc}",
                "ensure Playwright browsers are installed via 'playwright install chromium'",
            )
            raise

        vp = self._viewport
        logger.info(
            "🔴 Browser launched — viewport %dx%d, user-data-dir=%s",
            vp["width"], vp["height"], _BROWSER_DATA_DIR,
        )
        logger.info(
            "📌 IMPORTANT: A browser window is now open. "
            "If you need to log in, the browser will prompt you."
        )

    async def stop(self) -> None:
        """Close the browser context and stop Playwright."""
        if self._context:
            try:
                await self._context.close()
            except Exception:
                pass
            self._context = None

        if self._playwright:
            try:
                await self._playwright.stop()
            except Exception:
                pass
            self._playwright = None

    # ── Page factory ──────────────────────────────────────────────────────────

    async def new_page(self) -> Page:
        """
        Open a new browser tab and inject the stealth init script.

        The stealth script runs before any site JavaScript, so anti-bot checks
        that run on page load see a 'real' browser environment.
        """
        if self._context is None:
            raise RuntimeError(
                "BrowserManager.start() must be called before new_page(). "
                "Use 'async with BrowserManager(settings) as browser:'"
            )
        page = await self._context.new_page()
        await page.add_init_script(_STEALTH_INIT_SCRIPT)
        return page

    # ── Utilities ─────────────────────────────────────────────────────────────

    async def random_delay(self, multiplier: float = 1.0) -> None:
        """
        Sleep for a random duration between MIN_DELAY_MS and MAX_DELAY_MS.

        Call between every significant browser action (navigation, click, fill)
        to avoid triggering rate-limiting or bot-detection heuristics.

        Args:
            multiplier: Scale the delay range by this factor.
                        Use >1.0 after navigating to a heavy page, <1.0 for
                        lightweight interactions like scrolling.
        """
        lo = self.settings.min_delay_ms * multiplier
        hi = self.settings.max_delay_ms * multiplier
        delay_ms = random.uniform(lo, hi)
        await asyncio.sleep(delay_ms / 1000.0)

    # ── Context manager ────────────────────────────────────────────────────────

    async def __aenter__(self) -> "BrowserManager":
        await self.start()
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: Any,
    ) -> None:
        await self.stop()
