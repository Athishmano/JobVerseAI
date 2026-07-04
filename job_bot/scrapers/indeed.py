"""
┌─ FILE: job_bot/scrapers/indeed.py
├─ PURPOSE: Indeed-specific scraper implementation.
├─ USED BY: services/pipeline.py
├─ DATA FLOW: search() → page navigation → extract jobs → list[Job]
├─ DESIGN DECISIONS: Uses Playwright locators for Indeed. Handled Next button
│                    pagination.
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

class IndeedScraper(BaseScraper):
    @property
    def portal_name(self) -> str:
        return "Indeed"

    @property
    def portal_id(self) -> str:
        return "indeed"

    @property
    def base_url(self) -> str:
        return "https://www.indeed.com"

    async def is_logged_in(self, page: "Page") -> bool:
        # Check for user avatar or profile icon
        try:
            return await page.locator("a[aria-label='Profile'], [data-gnav-element-name='Profile']").count() > 0
        except Exception:
            return False

    async def search(self, page: "Page", query: str, location: str, limit: int) -> list[Job]:
        jobs = []
        page_num = 1
        start = 0

        q_enc = quote_plus(query)
        l_enc = quote_plus(location)

        while len(jobs) < limit:
            url = f"https://in.indeed.com/jobs?q={q_enc}&l={l_enc}&start={start}"
            
            try:
                await page.goto(url, timeout=30000)
                await self.browser.random_delay(1.5)
                
                try:
                    # Wait for job cards
                    await page.wait_for_selector(".job_seen_beacon, .mosaic-empty-results", timeout=15000)
                except Exception:
                    log_error(logger, f"{self.portal_id}.py", "search",
                              f"Timeout waiting for results on page {page_num}", "skipping page, continuing")
                    break

                job_elements = await page.locator(".job_seen_beacon").all()
                if not job_elements:
                    break

                for el in job_elements:
                    if len(jobs) >= limit:
                        break

                    try:
                        title_el = el.locator("h2.jobTitle a")
                        title = await title_el.inner_text()
                        
                        apply_url_path = await title_el.get_attribute("href") or ""
                        apply_url = f"https://in.indeed.com{apply_url_path}" if apply_url_path.startswith("/") else apply_url_path
                        
                        job_id = await el.locator("h2.jobTitle a").get_attribute("data-jk")
                        if not job_id:
                            job_id = f"ind-{len(jobs)}-{page_num}"

                        company = await el.locator("[data-testid='company-name']").inner_text()
                        
                        try:
                            loc = await el.locator("[data-testid='text-location']").inner_text()
                        except Exception:
                            loc = location

                        try:
                            salary = await el.locator(".salary-snippet-container").inner_text()
                        except Exception:
                            salary = None

                        desc = ""
                        try:
                            desc_items = await el.locator(".job-snippet li").all_inner_texts()
                            desc = " ".join(desc_items)
                        except Exception:
                            pass
                            
                        try:
                            posted = await el.locator("[data-testid='myJobsStateDate']").inner_text()
                        except Exception:
                            posted = None

                        job = Job(
                            source=self.portal_id,
                            job_id=job_id,
                            title=title.strip(),
                            company=company.strip(),
                            location=loc.strip(),
                            salary=salary.strip() if salary else None,
                            experience_required=None,
                            description=desc.strip(),
                            apply_url=apply_url,
                            posted_date=posted.strip() if posted else None,
                            applicant_count=None,
                            skills_required=[]
                        )
                        jobs.append(job)
                    except Exception as e:
                        log_warning(logger, f"{self.portal_id}.py", "search",
                                  f"Failed to parse job on page {page_num}: {e}", "skipping job")
                        continue

                # Indeed pagination (usually 10 or 15 per page)
                start += 10
                page_num += 1

            except Exception as e:
                log_error(logger, f"{self.portal_id}.py", "search",
                          f"Failed to load page {page_num}: {e}", "skipping page, continuing")
                break
                
        return jobs
