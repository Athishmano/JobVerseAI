"""
┌─ FILE: job_bot/scrapers/wellfound.py
├─ PURPOSE: Wellfound-specific scraper implementation.
├─ USED BY: services/pipeline.py
├─ DATA FLOW: search() → page navigation → extract jobs → list[Job]
├─ DESIGN DECISIONS: Uses Playwright locators for Wellfound. Needs manual login
│                    since Wellfound heavily uses Cloudflare and blocks scraping.
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

class WellfoundScraper(BaseScraper):
    @property
    def portal_name(self) -> str:
        return "Wellfound"

    @property
    def portal_id(self) -> str:
        return "wellfound"

    @property
    def base_url(self) -> str:
        return "https://wellfound.com"

    async def is_logged_in(self, page: "Page") -> bool:
        # Check for user avatar indicating logged in state
        try:
            return await page.locator("[data-test='Avatar']").count() > 0
        except Exception:
            return False

    async def search(self, page: "Page", query: str, location: str, limit: int) -> list[Job]:
        jobs = []
        page_num = 1

        # Wellfound search URL is usually complex, a simpler approach is using their /jobs URL
        # For simplicity, we use the role search URL but Wellfound might require GraphQL 
        # or specific interaction if simple URLs don't work.
        q_enc = quote_plus(query)
        l_enc = quote_plus(location)

        while len(jobs) < limit:
            url = f"https://wellfound.com/jobs?search={q_enc}&location={l_enc}&page={page_num}"
            
            try:
                await page.goto(url, timeout=30000)
                await self.browser.random_delay(2.0)
                
                try:
                    # Wait for job cards
                    await page.wait_for_selector("[data-test='StartupResult'], .styles_component__yB2E8", timeout=15000)
                except Exception:
                    log_error(logger, f"{self.portal_id}.py", "search",
                              f"Timeout waiting for results on page {page_num}", "skipping page, continuing")
                    break

                job_elements = await page.locator("[data-test='StartupResult'], .styles_component__yB2E8").all()
                if not job_elements:
                    break

                for el in job_elements:
                    if len(jobs) >= limit:
                        break

                    try:
                        title_el = el.locator(".styles_title__pYeih, [data-test='JobTitle']")
                        if await title_el.count() == 0:
                            continue
                            
                        title = await title_el.first.inner_text()
                        
                        apply_url_path = await title_el.first.get_attribute("href") or ""
                        apply_url = f"https://wellfound.com{apply_url_path}" if apply_url_path.startswith("/") else apply_url_path
                        
                        job_id = apply_url.split("/")[-1] if apply_url else f"wf-{len(jobs)}-{page_num}"

                        company_el = el.locator(".styles_name__mNHt4, [data-test='StartupName']")
                        company = await company_el.first.inner_text() if await company_el.count() > 0 else "Unknown"
                        
                        loc = location # Wellfound location is usually in a pill list
                        try:
                            loc_pills = await el.locator(".styles_pill__UuA91").all_inner_texts()
                            if loc_pills:
                                loc = loc_pills[0]
                        except Exception:
                            pass

                        salary = None
                        try:
                            salary_el = el.locator(".styles_salary__T3R6m")
                            if await salary_el.count() > 0:
                                salary = await salary_el.first.inner_text()
                        except Exception:
                            pass

                        desc = ""
                        try:
                            desc_el = el.locator(".styles_description__2Y8lE")
                            if await desc_el.count() > 0:
                                desc = await desc_el.first.inner_text()
                        except Exception:
                            pass
                            
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
                            posted_date=posted,
                            applicant_count=None,
                            skills_required=[]
                        )
                        jobs.append(job)
                    except Exception as e:
                        log_warning(logger, f"{self.portal_id}.py", "search",
                                  f"Failed to parse job on page {page_num}: {e}", "skipping job")
                        continue

                # Pagination
                page_num += 1

            except Exception as e:
                log_error(logger, f"{self.portal_id}.py", "search",
                          f"Failed to load page {page_num}: {e}", "skipping page, continuing")
                break
                
        return jobs
