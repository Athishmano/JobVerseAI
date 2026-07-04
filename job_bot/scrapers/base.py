"""
┌─ FILE: job_bot/scrapers/base.py
├─ PURPOSE: Abstract base class that every portal scraper extends.
│           Provides the shared run() orchestration (login check, combo iteration,
│           error isolation, limit enforcement) so individual scrapers only implement
│           portal-specific DOM logic.
├─ USED BY: scrapers/naukri.py, linkedin.py, indeed.py, wellfound.py
│           services/pipeline.py (calls scraper.run(limit) for each active portal)
├─ DATA FLOW: profile.job_preferences.titles × locations → combos →
│             is_logged_in() / _wait_for_manual_login() →
│             search(page, query, location, remaining_limit) → list[Job]
├─ DESIGN DECISIONS: run() is concrete so every scraper gets identical login-wait,
│                    combo-logging, CAPTCHA detection, and error-isolation behaviour
│                    for free. Scrapers only implement the portal-specific is_logged_in()
│                    and search() methods. portal_id drives all log prefixes so errors
│                    are always traceable to the exact scraper file.
└─ PATTERNS: ABC, template method pattern, fail-local error handling, async context
"""

from __future__ import annotations

import asyncio
import logging
from abc import ABC, abstractmethod
from typing import TYPE_CHECKING

from job_bot.core.config import Settings
from job_bot.core.logger import log_error, log_warning
from job_bot.schemas.job import Job
from job_bot.schemas.profile import UserProfile

if TYPE_CHECKING:
    from playwright.async_api import Page

    from job_bot.core.browser import BrowserManager

logger = logging.getLogger(__name__)

# Seconds between is_logged_in() polls during manual login wait
_LOGIN_POLL_INTERVAL: float = 2.0

# Text patterns that indicate a CAPTCHA challenge (case-insensitive)
_CAPTCHA_INDICATORS: tuple[str, ...] = (
    "verify you are human",
    "i am not a robot",
    "security check",
    "complete the captcha",
    "ddos protection",
    "ray id",            # Cloudflare
    "cf-challenge",
)


class BaseScraper(ABC):
    """
    Abstract base for all portal scrapers.

    Subclasses MUST implement:
        portal_name  (property) → str   e.g. "Naukri"
        portal_id    (property) → str   e.g. "naukri"
        is_logged_in(page)      → bool
        search(page, query, location, limit) → list[Job]

    Subclasses get for free:
        run(limit) — full orchestration with login-wait + combo iteration
        _wait_for_manual_login(page)
        _detect_captcha(page)
    """

    def __init__(
        self,
        browser_manager: "BrowserManager",
        profile: UserProfile,
        settings: Settings,
    ) -> None:
        self.browser = browser_manager
        self.profile = profile
        self.settings = settings

    # ── Abstract interface ─────────────────────────────────────────────────────

    @property
    @abstractmethod
    def portal_name(self) -> str:
        """
        Human-readable portal name used in log messages.
        Example: 'Naukri', 'LinkedIn', 'Indeed', 'Wellfound'
        """
        ...

    @property
    @abstractmethod
    def portal_id(self) -> str:
        """
        Lowercase slug used as the log-error filename prefix.
        Example: 'naukri', 'linkedin', 'indeed', 'wellfound'
        Must match the scraper's actual filename: {portal_id}.py
        """
        ...

    @property
    @abstractmethod
    def base_url(self) -> str:
        """
        The root URL of the portal (e.g. 'https://www.naukri.com').
        The scraper will navigate here to check the initial login state.
        """
        ...

    @abstractmethod
    async def is_logged_in(self, page: "Page") -> bool:
        """
        Return True if the browser's current session shows a logged-in state
        for this portal (e.g., profile avatar or username visible in the nav).

        Should be lightweight — only check DOM, never navigate away.
        Never raises; return False on any unexpected error.
        """
        ...

    @abstractmethod
    async def search(
        self,
        page: "Page",
        query: str,
        location: str,
        limit: int,
    ) -> list[Job]:
        """
        Navigate to the portal's search, paginate until *limit* jobs are
        collected or no more pages exist, and return all extracted jobs.

        Responsibility of the implementer:
        - Log each page navigation in the format:
              [Portal N/Total] 'query' in 'location' — page X
          (page 1 is logged by run(); subsequent pages logged here)
        - Set applicant_count to None when the portal does not expose it
        - Catch page-level exceptions, log with log_error(), and continue
        - Respect *limit* — stop pagination once reached

        Args:
            page:     Active Playwright page (already has stealth applied)
            query:    Job title / search term
            location: Location string from profile.job_preferences.locations
            limit:    Maximum number of jobs to return from this combo

        Returns:
            List of Job objects extracted from this search combo.
        """
        ...

    # ── Concrete helpers ───────────────────────────────────────────────────────

    async def _wait_for_manual_login(self, page: "Page") -> bool:
        """
        Prompt the user to log in manually in the visible browser window,
        then poll is_logged_in() every ~2 seconds until the session is detected
        or LOGIN_WAIT_TIMEOUT_SECONDS is exhausted.

        Returns True if login succeeds within the timeout, False otherwise.

        Logs (matching spec output exactly):
            👉 Not logged in to Naukri. Please log in now in the browser window.
               (Use Google, email/password, or any method you prefer)
               Waiting up to 5 minutes for login to complete...
            ✓ Naukri login detected (nav elements found)! Continuing...
        """
        logger.info(
            "👉 Not logged in to %s. Please log in now in the browser window.",
            self.portal_name,
        )
        logger.info("   (Use Google, email/password, or any method you prefer)")
        logger.info(
            "   Waiting up to %d minutes for login to complete...",
            self.settings.login_wait_timeout_seconds // 60,
        )

        elapsed = 0.0
        while elapsed < self.settings.login_wait_timeout_seconds:
            await asyncio.sleep(_LOGIN_POLL_INTERVAL)
            elapsed += _LOGIN_POLL_INTERVAL
            try:
                if await self.is_logged_in(page):
                    logger.info(
                        "✓ %s login detected (nav elements found)! Continuing...",
                        self.portal_name,
                    )
                    return True
            except Exception:
                pass  # page may still be mid-redirect during login — ignore

        return False

    async def _detect_captcha(self, page: "Page") -> bool:
        """
        Heuristic CAPTCHA / bot-challenge detection.

        Checks for:
        - reCAPTCHA iframes (Google, hCaptcha)
        - Cloudflare challenge form
        - CAPTCHA-related text in the page body

        Returns True if a CAPTCHA is likely present (caller should pause / skip).
        Returns False on any exception — false negatives are safer than crashes.
        """
        try:
            # iFrame-based CAPTCHAs
            for selector in (
                "iframe[src*='recaptcha']",
                "iframe[src*='hcaptcha']",
                "iframe[title*='reCAPTCHA']",
            ):
                if await page.query_selector(selector):
                    return True

            # Cloudflare challenge page
            if await page.query_selector("#challenge-form"):
                return True

            # Text-based heuristics — check first 2 000 chars of body text
            try:
                body_text = (await page.inner_text("body", timeout=2_000))[:2_000].lower()
                if any(indicator in body_text for indicator in _CAPTCHA_INDICATORS):
                    return True
            except Exception:
                pass

        except Exception:
            pass  # CAPTCHA detection is best-effort — never crash the scraper

        return False

    # ── Orchestration ──────────────────────────────────────────────────────────

    async def run(self, limit: int) -> list[Job]:
        """
        Full scraping run for this portal.

        Steps:
        1. Log section header: ═══ Scraping NAUKRI ═══
        2. Open a new stealth browser page and navigate to base_url
        3. Check is_logged_in():
           - If not logged in → _wait_for_manual_login() → skip on timeout
        4. Iterate every (title × location) combo from profile.job_preferences
        5. For each combo: detect CAPTCHA, call search(), log progress, isolate errors
        6. Stop early once *limit* jobs are collected across all combos
        7. Close the page regardless of outcome

        Returns:
            All Job objects collected across all combos (may be < limit on errors).
        """
        logger.info("═══ Scraping %s ═══", self.portal_name.upper())

        page = await self.browser.new_page()
        all_jobs: list[Job] = []

        try:
            # Navigate to the portal's homepage first
            try:
                await page.goto(self.base_url, timeout=30000)
                await self.browser.random_delay()
            except Exception as exc:
                log_warning(
                    logger, f"{self.portal_id}.py", "run",
                    f"Could not load {self.base_url}: {exc}",
                    "proceeding anyway"
                )

            # ── Step 1: Login check ───────────────────────────────────────────
            try:
                logged_in = await self.is_logged_in(page)
            except Exception as exc:
                log_warning(
                    logger, f"{self.portal_id}.py", "run",
                    f"Could not determine login state: {exc}",
                    "proceeding without confirmed login",
                )
                logged_in = True  # attempt anyway — portal may allow guest browsing

            if not logged_in:
                # If the user didn't even provide credentials in .env, assume they 
                # intend to scrape as a guest and skip the 5-minute wait entirely.
                username = getattr(self.settings, f"{self.portal_id}_username", "")
                password = getattr(self.settings, f"{self.portal_id}_password", "")
                
                if not username and not password:
                    logger.info("ℹ️ No credentials provided for %s. Skipping manual login wait and proceeding as guest.", self.portal_name)
                    # Proceed as guest
                else:
                    logged_in = await self._wait_for_manual_login(page)
                    if not logged_in:
                        log_error(
                            logger, f"{self.portal_id}.py", "login",
                            f"Login not detected after "
                            f"{self.settings.login_wait_timeout_seconds // 60} minutes",
                            f"skipping {self.portal_name} for this run",
                        )
                        return []

            # ── Step 2: Combo iteration ───────────────────────────────────────
            titles    = self.profile.job_preferences.titles
            locations = self.profile.job_preferences.locations
            total_combos = len(titles) * len(locations)
            combo_num    = 0
            jobs_collected = 0

            outer_break = False
            for title in titles:
                if outer_break:
                    break
                for location in locations:
                    if jobs_collected >= limit:
                        logger.info(
                            "Per-site limit of %d reached for %s — stopping early",
                            limit, self.portal_name,
                        )
                        outer_break = True
                        break

                    combo_num += 1
                    remaining = limit - jobs_collected

                    # Page-1 log line (matching spec format exactly)
                    logger.info(
                        "[%s %d/%d] '%s' in '%s' — page 1",
                        self.portal_name, combo_num, total_combos, title, location,
                    )

                    try:
                        # CAPTCHA guard before each search combo
                        if await self._detect_captcha(page):
                            log_error(
                                logger, f"{self.portal_id}.py", "run",
                                f"CAPTCHA detected for '{title}' in '{location}'",
                                "skipping combo, run continues",
                            )
                            await asyncio.sleep(5.0)
                            continue

                        combo_jobs = await self.search(page, title, location, remaining)

                    except Exception as exc:
                        log_error(
                            logger, f"{self.portal_id}.py", "run",
                            f"Search failed for '{title}' in '{location}': {exc}",
                            "skipping combo, continuing",
                        )
                        continue

                    new_count = len(combo_jobs)
                    all_jobs.extend(combo_jobs)
                    jobs_collected += new_count

                    if new_count:
                        logger.info(
                            "  +%d new %s jobs (total: %d)",
                            new_count, self.portal_name, jobs_collected,
                        )

        finally:
            # Always close the page — even if an unexpected exception propagated
            try:
                await page.close()
            except Exception:
                pass

        logger.info(
            "Finished %s — %d jobs collected across %d combos",
            self.portal_name, len(all_jobs), combo_num if "combo_num" in dir() else 0,
        )
        return all_jobs
