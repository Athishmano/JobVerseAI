"""
┌─ FILE: job_bot/scrapers/linkedin.py
├─ PURPOSE: LinkedIn-specific scraper implementation.
├─ USED BY: services/pipeline.py
├─ DATA FLOW: search() → page navigation → extract jobs → list[Job]
├─ DESIGN DECISIONS: Uses Playwright locators for LinkedIn job search. Relies on
│                    URL parameters for queries. Tries to extract applicant_count
│                    since LinkedIn usually exposes it.
└─ PATTERNS: Inheritance (BaseScraper), Try/except for error isolation.
"""

import asyncio
import logging
from typing import TYPE_CHECKING
from urllib.parse import quote_plus

from job_bot.core.logger import log_error, log_warning
from job_bot.schemas.job import Job
from job_bot.scrapers.base import BaseScraper

if TYPE_CHECKING:
    from playwright.async_api import Page

logger = logging.getLogger(__name__)

class LinkedInScraper(BaseScraper):
    @property
    def portal_name(self) -> str:
        return "LinkedIn"

    @property
    def portal_id(self) -> str:
        return "linkedin"

    @property
    def base_url(self) -> str:
        return "https://www.linkedin.com"

    async def is_logged_in(self, page: "Page") -> bool:
        # Check for user avatar or typical logged-in nav elements
        try:
            return await page.locator(".global-nav__me-photo, .global-nav__nav").count() > 0
        except Exception:
            return False

    async def search(self, page: "Page", query: str, location: str, limit: int) -> list[Job]:
        jobs = []
        page_num = 1
        start = 0

        # Base LinkedIn job search URL with query and location
        q_enc = quote_plus(query)
        l_enc = quote_plus(location)

        while len(jobs) < limit:
            url = f"https://www.linkedin.com/jobs/search/?keywords={q_enc}&location={l_enc}&start={start}"
            
            try:
                await page.goto(url, timeout=30000)
                await self.browser.random_delay(1.5)
                
                try:
                    # Wait for either job list or empty state
                    await page.wait_for_selector(".jobs-search-results__list-item, .jobs-search-two-pane__no-results", timeout=15000)
                except Exception:
                    log_error(logger, f"{self.portal_id}.py", "search",
                              f"Timeout waiting for results on page {page_num}", "skipping page, continuing")
                    break

                job_elements = await page.locator(".jobs-search-results__list-item").all()
                if not job_elements:
                    break

                for el in job_elements:
                    if len(jobs) >= limit:
                        break

                    try:
                        title_el = el.locator(".job-card-list__title, .job-card-container__title")
                        if await title_el.count() == 0:
                            continue
                            
                        title = await title_el.first.inner_text()
                        
                        apply_url_path = await title_el.first.get_attribute("href") or ""
                        apply_url = f"https://www.linkedin.com{apply_url_path}" if apply_url_path.startswith("/") else apply_url_path
                        
                        job_id = ""
                        if apply_url and "/view/" in apply_url:
                            parts = apply_url.split("/view/")
                            if len(parts) > 1:
                                job_id = parts[1].split("/")[0]
                        if not job_id:
                            job_id = f"li-{len(jobs)}-{page_num}"

                        try:
                            company = await el.locator(".job-card-container__primary-description").inner_text()
                        except Exception:
                            company = ""
                        
                        try:
                            loc = await el.locator(".job-card-container__metadata-item").first.inner_text()
                        except Exception:
                            loc = location

                        # Instead of clicking and waiting for the details pane, 
                        # we extract available metadata from the card itself to match Naukri's speed.
                        try:
                            card_text = await el.inner_text()
                            desc = card_text.replace('\n', ' ').strip()
                        except Exception:
                            desc = ""

                        job = Job(
                            source=self.portal_id,
                            job_id=job_id,
                            title=title.strip(),
                            company=company.strip(),
                            location=loc.strip(),
                            salary=None,
                            experience_required=None,
                            description=desc,
                            apply_url=apply_url,
                            posted_date=None,
                            applicant_count=None,
                            skills_required=[]
                        )
                        jobs.append(job)
                    except Exception as e:
                        log_warning(logger, f"{self.portal_id}.py", "search",
                                  f"Failed to parse job on page {page_num}: {e}", "skipping job")
                        continue

                # LinkedIn pagination (25 jobs per page usually)
                start += 25
                page_num += 1

            except Exception as e:
                log_error(logger, f"{self.portal_id}.py", "search",
                          f"Failed to load page {page_num}: {e}", "skipping page, continuing")
                break
                
        return jobs

    async def run(self, limit: int) -> list[Job]:
        """
        LinkedIn-specific run() override (matching Naukri's strategy).
        
        Strategy:
        1. Combine ALL profile locations into one multi-city search per title.
        2. Scrape jobs quickly by extracting from cards without clicking them.
        """
        logger.info("═══ Scraping %s ═══", self.portal_name.upper())

        page = await self.browser.new_page()
        all_jobs: list[Job] = []
        seen_ids: set[str] = set()

        # Scrape up to 5 pages per title just like Naukri
        max_pages_per_title = 5

        try:
            try:
                await page.goto(self.base_url, timeout=30_000)
                await self.browser.random_delay()
            except Exception as exc:
                log_warning(logger, f"{self.portal_id}.py", "run",
                            f"Could not load {self.base_url}: {exc}", "proceeding anyway")

            try:
                logged_in = await self.is_logged_in(page)
            except Exception:
                logged_in = True

            if not logged_in:
                username = getattr(self.settings, f"{self.portal_id}_username", "")
                password = getattr(self.settings, f"{self.portal_id}_password", "")
                if not username and not password:
                    logger.info("ℹ️ No credentials for %s. Proceeding as guest.", self.portal_name)
                else:
                    logged_in = await self._wait_for_manual_login(page)
                    if not logged_in:
                        log_error(logger, f"{self.portal_id}.py", "login",
                                  "Login timed out", f"skipping {self.portal_name}")
                        return []

            # Combine locations (exclude Remote since it can be handled by location params if needed)
            locations = self.profile.job_preferences.locations
            non_remote = [loc for loc in locations if loc.lower() != "remote"]
            combined_locs = ", ".join(non_remote) if non_remote else (locations[0] if locations else "Worldwide")

            titles = self.profile.job_preferences.titles
            combo_num = 0

            for title in titles:
                combo_num += 1
                logger.info(
                    "[%s %d/%d] '%s' across [%s] — up to %d pages",
                    self.portal_name, combo_num, len(titles),
                    title, combined_locs, max_pages_per_title,
                )

                try:
                    if await self._detect_captcha(page):
                        log_error(logger, f"{self.portal_id}.py", "run",
                                  f"CAPTCHA detected for '{title}'", "skipping title")
                        await asyncio.sleep(5.0)
                        continue

                    # limit=999_999 to get all jobs from the allowed pages
                    title_jobs = await self.search(page, title, combined_locs, limit=max_pages_per_title * 25)

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
                    log_error(logger, f"{self.portal_id}.py", "run",
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

