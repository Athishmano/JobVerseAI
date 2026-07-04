"""
┌─ FILE: job_bot/scrapers/naukri.py
├─ PURPOSE: Naukri.com scraper — collects ALL available fresher/intern jobs
│           across all configured cities in a single combined search per title.
├─ USED BY: services/pipeline.py (via scraper.run())
├─ DATA FLOW: run() → search(page, title, all_locs, limit=∞) → list[Job]
├─ DESIGN DECISIONS:
│   • Overrides BaseScraper.run() to merge all profile locations into ONE
│     Naukri search query per title. This mirrors how the Naukri UI works
│     when you manually select multiple cities — it returns a richer,
│     better-ranked result set than N separate single-city queries.
│   • Quality URL parameters match a refined manual search:
│       experience=0   → fresher only
│       jobAge=15      → last 15 days
│       wfhType=0      → includes WFH/remote
│       glbl_qcrc=1028 → IT sector filter
│       ugTypeGid=12   → undergrad/fresher type
│       cityTypeGid=*  → city precision IDs for each selected city
│       sortBy=date    → most recent first
│   • Scrapes up to _MAX_PAGES_PER_TITLE pages per title, then hands ALL
│     collected jobs back to the pipeline which does offline rank → AI score
│     → top-N selection. The scraper itself applies NO limit on what it returns.
└─ PATTERNS: Inheritance override (run()), multi-location URL builder.
"""

from __future__ import annotations

import asyncio
import logging
import re
from typing import TYPE_CHECKING
from urllib.parse import quote_plus

from job_bot.core.logger import log_error, log_warning
from job_bot.schemas.job import Job
from job_bot.scrapers.base import BaseScraper

if TYPE_CHECKING:
    from playwright.async_api import Page

logger = logging.getLogger(__name__)

# ── Constants ──────────────────────────────────────────────────────────────────

# Scrape up to this many pages per job title.
# ~20 jobs/page × 5 pages × 5 titles = ~500 raw candidates.
_MAX_PAGES_PER_TITLE = 5

# Naukri internal city precision IDs — improves search result quality.
# Add more cities as needed.
_CITY_GIDS: dict[str, str] = {
    "coimbatore": "97",
    "chennai":    "183",
    "bangalore":  "184",
    "bengaluru":  "184",
    "hyderabad":  "174",
    "mumbai":     "187",
    "delhi":      "130",
    "pune":       "188",
    "noida":      "186",
    "gurugram":   "132",
    "gurgaon":    "132",
    "kolkata":    "189",
    "ahmedabad":  "170",
    "jaipur":     "178",
}


class NaukriScraper(BaseScraper):

    @property
    def portal_name(self) -> str:
        return "Naukri"

    @property
    def portal_id(self) -> str:
        return "naukri"

    @property
    def base_url(self) -> str:
        return "https://www.naukri.com"

    # ── Login check ────────────────────────────────────────────────────────────

    async def is_logged_in(self, page: "Page") -> bool:
        try:
            return await page.locator(
                ".nI-gNb-drawer, .nI-gNb-header__usermenu, "
                ".nI-gNb-sb__main, .user-info, img.nI-gNb-header__userImg, "
                ".nI-gNb-drawer__icon, .nI-gNb-header__avatar"
            ).count() > 0
        except Exception:
            return False

    # ── URL builder ────────────────────────────────────────────────────────────

    def _build_search_url(self, keywords: str, locations_str: str, page_num: int = 1) -> str:
        """
        Build a quality-optimised Naukri search URL.

        Key improvements over a plain search:
        - All locations combined in one `l=` parameter (mirrors manual multi-city search)
        - experience=0  → freshers only
        - jobAge=15     → last 15 days (broader window than default)
        - wfhType=0     → includes work-from-home jobs
        - glbl_qcrc=1028 → IT / Software sector filter
        - ugTypeGid=12  → undergrad type (fresher-friendly roles)
        - cityTypeGid   → precision city IDs for each selected location
        - sortBy=date   → most recent listings first
        """
        kw_slug = keywords.lower().replace(" ", "-")

        # Use first location for URL path slug, all for the `l=` query param
        locs = [loc.strip() for loc in locations_str.split(",") if loc.strip()]
        first_loc = locs[0] if locs else "india"
        first_loc_slug = first_loc.lower().replace(" ", "-")

        # Build cityTypeGid params for each city we recognise
        city_gid_params = "".join(
            f"&cityTypeGid={_CITY_GIDS[loc.lower()]}"
            for loc in locs
            if loc.lower() in _CITY_GIDS
        )

        url = (
            f"https://www.naukri.com/{kw_slug}-jobs-in-{first_loc_slug}"
            f"?k={quote_plus(keywords)}"
            f"&l={quote_plus(locations_str)}"
            f"&experience=0"
            f"&jobAge=15"
            f"&wfhType=0"
            f"&glbl_qcrc=1028"
            f"&ugTypeGid=12"
            f"&sortBy=date"
            f"{city_gid_params}"
        )

        if page_num > 1:
            url += f"&pageNo={page_num}"

        return url

    # ── Page card extractor ────────────────────────────────────────────────────

    async def search(self, page: "Page", query: str, location: str, limit: int) -> list[Job]:
        """
        Paginate through Naukri results for (query, location), collecting up to
        `limit` jobs OR _MAX_PAGES_PER_TITLE pages — whichever comes first.

        When called from our run() override, limit=999_999 so effectively all
        pages up to _MAX_PAGES_PER_TITLE are scraped.
        """
        jobs: list[Job] = []
        seen_ids: set[str] = set()
        page_num = 1

        while len(jobs) < limit:
            # Page cap guard
            if page_num > _MAX_PAGES_PER_TITLE:
                logger.info("  Reached page cap (%d) for '%s'", _MAX_PAGES_PER_TITLE, query)
                break

            url = self._build_search_url(query, location, page_num)

            try:
                await page.goto(url, wait_until="domcontentloaded", timeout=30_000)
                await asyncio.sleep(2)

                # Scroll to trigger lazy-loaded cards
                await page.evaluate("window.scrollBy(0, 400)")
                await asyncio.sleep(0.5)
                await page.evaluate("window.scrollBy(0, 400)")
                await asyncio.sleep(0.5)

                # Locate job cards — try multiple Naukri layout selectors
                cards = page.locator("article.jobTuple, .srp-jobtuple-wrapper, .cust-job-tuple")
                count = await cards.count()

                if count == 0:
                    # Naukri v2 / alternate layout
                    cards = page.locator("[data-job-id], .styles_jlc__main__VdwtF")
                    count = await cards.count()

                if count == 0:
                    logger.info("  No more results on page %d for '%s' — stopping", page_num, query)
                    break

                logger.info("  [Page %d] Found %d cards for '%s'", page_num, count, query)

                for i in range(count):
                    if len(jobs) >= limit:
                        break

                    try:
                        card = cards.nth(i)

                        # Title + URL
                        title_el = card.locator("a.title, a[class*='title'], .row1 a, .info h2 a")
                        title = ""
                        href  = ""
                        if await title_el.count() > 0:
                            title = (await title_el.first.inner_text()).strip()
                            href  = (await title_el.first.get_attribute("href")) or ""

                        # Company
                        comp_el = card.locator("a.subTitle, .comp-name, a[class*='comp'], .row2 span a")
                        company = (await comp_el.first.inner_text()).strip() if await comp_el.count() > 0 else ""

                        # Location
                        loc_el = card.locator(".locWdth, .loc-wrap, span[class*='loc'], .row3 .loc span")
                        location_text = (await loc_el.first.inner_text()).strip() if await loc_el.count() > 0 else location

                        # Job ID
                        job_id_match = re.search(r"-(\d{10,})\??", href)
                        if job_id_match:
                            job_id = job_id_match.group(1)
                        else:
                            job_id = (
                                await card.get_attribute("data-job-id")
                                or await card.get_attribute("id")
                                or f"naukri_unknown_{page_num}_{i}"
                            )

                        if job_id in seen_ids:
                            continue
                        seen_ids.add(job_id)

                        apply_url = href if href.startswith("http") else f"https://www.naukri.com{href}"

                        # Experience
                        try:
                            exp_el = card.locator(".expwdth, span[class*='exp'], .row3 .exp span")
                            exp = (await exp_el.first.inner_text()).strip() if await exp_el.count() > 0 else None
                        except Exception:
                            exp = None

                        # Salary
                        try:
                            sal_el = card.locator(".sal, span[class*='sal'], .row3 .sal span")
                            salary = (await sal_el.first.inner_text()).strip() if await sal_el.count() > 0 else None
                        except Exception:
                            salary = None

                        # Posted date
                        try:
                            posted_el = card.locator(".job-post-day, span[class*='post']")
                            posted = (await posted_el.first.inner_text()).strip() if await posted_el.count() > 0 else None
                        except Exception:
                            posted = None

                        # Tags / description snippet
                        try:
                            tag_texts = await card.locator(".tags-gt, ul.tags li").all_inner_texts()
                            desc = " ".join(tag_texts).strip()
                        except Exception:
                            desc = ""

                        jobs.append(Job(
                            source=self.portal_id,
                            job_id=job_id,
                            title=title,
                            company=company,
                            location=location_text,
                            salary=salary,
                            experience_required=exp,
                            description=desc,
                            apply_url=apply_url,
                            posted_date=posted,
                            applicant_count=None,
                            skills_required=[],
                        ))

                    except Exception as exc:
                        log_warning(
                            logger, "naukri.py", "search",
                            f"Failed to parse card {i} on page {page_num}: {exc}",
                            "skipping card",
                        )
                        continue

                page_num += 1

            except Exception as exc:
                log_error(
                    logger, "naukri.py", "search",
                    f"Failed to load page {page_num} for '{query}': {exc}",
                    "stopping pagination for this title",
                )
                break

        return jobs

    # ── Orchestration override ─────────────────────────────────────────────────

    async def run(self, limit: int) -> list[Job]:
        """
        Naukri-specific run() override.

        Strategy:
        1. Combine ALL profile locations into one multi-city search per title.
           e.g. locations=["Coimbatore","Chennai","Bangalore","Remote"] →
                l=Coimbatore, Chennai, Bangalore   (Remote handled via wfhType=0)
        2. Scrape up to _MAX_PAGES_PER_TITLE pages per title — no per-combo limit.
        3. Return ALL unique jobs. The pipeline handles top-N selection via
           offline ranking + AI scoring.

        The `limit` parameter is preserved for the base-class interface signature
        but is deliberately not used to cap scraping here — ranking happens later.
        """
        logger.info("═══ Scraping %s ═══", self.portal_name.upper())

        page = await self.browser.new_page()
        all_jobs: list[Job] = []
        seen_ids: set[str] = set()

        try:
            # Navigate to portal homepage
            try:
                await page.goto(self.base_url, timeout=30_000)
                await self.browser.random_delay()
            except Exception as exc:
                log_warning(logger, "naukri.py", "run",
                            f"Could not load {self.base_url}: {exc}", "proceeding anyway")

            # Login check
            try:
                logged_in = await self.is_logged_in(page)
            except Exception:
                logged_in = True  # assume guest browsing works

            if not logged_in:
                username = getattr(self.settings, "naukri_username", "")
                password = getattr(self.settings, "naukri_password", "")
                if not username and not password:
                    logger.info(
                        "ℹ️ No credentials for %s. Proceeding as guest.",
                        self.portal_name,
                    )
                else:
                    logged_in = await self._wait_for_manual_login(page)
                    if not logged_in:
                        log_error(logger, "naukri.py", "login",
                                  "Login timed out", f"skipping {self.portal_name}")
                        return []

            # Build a single combined location string (exclude "Remote" — covered by wfhType=0)
            locations = self.profile.job_preferences.locations
            non_remote = [loc for loc in locations if loc.lower() != "remote"]
            combined_locs = ", ".join(non_remote) if non_remote else (locations[0] if locations else "India")

            titles = self.profile.job_preferences.titles
            combo_num = 0

            for title in titles:
                combo_num += 1
                logger.info(
                    "[%s %d/%d] '%s' across [%s] — up to %d pages",
                    self.portal_name, combo_num, len(titles),
                    title, combined_locs, _MAX_PAGES_PER_TITLE,
                )

                try:
                    if await self._detect_captcha(page):
                        log_error(logger, "naukri.py", "run",
                                  f"CAPTCHA detected for '{title}'", "skipping title")
                        await asyncio.sleep(5.0)
                        continue

                    # Pass a very large limit so search() scrapes all pages up to _MAX_PAGES_PER_TITLE
                    title_jobs = await self.search(page, title, combined_locs, limit=999_999)

                    new_count = 0
                    for job in title_jobs:
                        if job.job_id not in seen_ids:
                            seen_ids.add(job.job_id)
                            all_jobs.append(job)
                            new_count += 1

                    if new_count:
                        logger.info("  +%d unique jobs from '%s' (total: %d)",
                                    new_count, title, len(all_jobs))
                    else:
                        logger.info("  No new jobs from '%s'", title)

                except Exception as exc:
                    log_error(logger, "naukri.py", "run",
                              f"Search failed for '{title}': {exc}", "skipping title")

        finally:
            try:
                await page.close()
            except Exception:
                pass

        logger.info(
            "Finished %s — %d unique raw jobs collected (pipeline will rank & AI-score top %d)",
            self.portal_name, len(all_jobs), limit,
        )
        return all_jobs
