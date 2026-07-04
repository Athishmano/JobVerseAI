"""
┌─ FILE: job_bot/services/pipeline.py
├─ PURPOSE: Orchestrates the entire scraping and scoring run.
├─ USED BY: main.py
├─ DATA FLOW:
│   1. Load state (Profile, Seen Jobs, Score Cache)
│   2. Init BrowserManager
│   3. Scrapers collect ALL available jobs (no per-site limit during scraping)
│   4. Phase 1 — Offline pre-filter: remove blacklisted / over-experienced jobs
│   5. Phase 2 — Offline rank: fast keyword scoring on pre-filtered jobs
│   6. Phase 3 — AI scoring: Gemini scores only the top `limit` offline candidates
│   7. Save results (Passed = score ≥ threshold, Failed = everything else)
├─ DESIGN DECISIONS:
│   • `--limit N` now means "AI-score the top N offline-ranked candidates".
│     Jobs outside the top N are rejected with "offline_rank_cut" so they
│     are still visible in the dashboard with a clear explanation.
│   • This ensures AI quota is spent on the most promising jobs, while the
│     scraper collects the full available pool for quality ranking.
│   • Fault-tolerant: errors in one scraper don't block others.
└─ PATTERNS: Async pipeline, Dependency Injection, two-pass scoring.
"""

import asyncio
import logging
from typing import Any

from job_bot.core.ai_scorer import AIScorer, QuotaExhaustedError
from job_bot.core.browser import BrowserManager
from job_bot.core.config import Settings
from job_bot.core.filter import apply_pre_filters
from job_bot.core.ranker import offline_rank
from job_bot.core.storage import (
    FailedRunData,
    PassedRunData,
    generate_run_timestamp,
    load_profile,
    load_score_cache,
    load_seen_jobs,
    save_failed,
    save_passed,
    save_score_cache,
    save_seen_jobs,
)
from job_bot.schemas.job import AIScore, Job, RejectedJob
from job_bot.scrapers.indeed import IndeedScraper
from job_bot.scrapers.linkedin import LinkedInScraper
from job_bot.scrapers.naukri import NaukriScraper
from job_bot.scrapers.wellfound import WellfoundScraper

logger = logging.getLogger(__name__)


def _make_rejected(job: Job, reason: str, detail: str, score: float | None = None) -> RejectedJob:
    """Helper to create a RejectedJob from a Job without repeating boilerplate."""
    return RejectedJob(
        **job.model_dump(exclude={"job_hash"}),
        rejection_reason=reason,
        rejection_detail=detail,
        ai_score=score,
    )


async def run_pipeline(settings: Settings, limit_per_site: int, sites: str = "all") -> str:
    """
    Execute the end-to-end job scraping and scoring pipeline.

    Args:
        settings:       Loaded environment / config values.
        limit_per_site: Number of top offline-ranked candidates to AI-score per run.
                        Scrapers collect ALL available jobs; this controls how many
                        of the best-ranked ones get the expensive Gemini call.
        sites:          Comma-separated portal IDs, or 'all'.

    Returns:
        Run timestamp string (used to open the dashboard).
    """
    ts = generate_run_timestamp()

    # ── 1. Load persistent state ──────────────────────────────────────────────
    profile = load_profile()
    seen_jobs = load_seen_jobs()
    score_cache = load_score_cache()

    initial_seen_count  = len(seen_jobs)
    initial_cache_count = len(score_cache)

    scorer = AIScorer(settings=settings, score_cache=score_cache)

    all_scraped_jobs: list[Job] = []
    sites_scraped:    list[str] = []

    logger.info(
        "🚀 Starting pipeline — AI scoring budget: top %d candidates | Sites: %s",
        limit_per_site, sites,
    )

    # ── 2. Scrape (all portals collect their full available pool) ─────────────
    async with BrowserManager(settings) as browser:
        all_scrapers = [
            NaukriScraper(browser, profile, settings),
            LinkedInScraper(browser, profile, settings),
            IndeedScraper(browser, profile, settings),
            WellfoundScraper(browser, profile, settings),
        ]

        if sites.lower() == "all":
            scrapers = all_scrapers
        else:
            allowed = {s.strip().lower() for s in sites.split(",")}
            scrapers = [s for s in all_scrapers if s.portal_id in allowed]

        if not scrapers:
            logger.warning("No valid scrapers found for sites: %s", sites)
            return ts

        for scraper in scrapers:
            try:
                # limit is passed but scrapers may scrape more; pipeline ranks top-N
                jobs = await scraper.run(limit=limit_per_site)
                if jobs:
                    sites_scraped.append(scraper.portal_id)
                    all_scraped_jobs.extend(jobs)
            except Exception as exc:
                logger.error(
                    "[%s.py:run] Fatal error running scraper: %s — skipping portal",
                    scraper.portal_id, exc,
                )

    total_scraped = len(all_scraped_jobs)
    logger.info("Total raw jobs scraped across all portals: %d", total_scraped)

    passed_jobs:   list[tuple[Job, AIScore]] = []
    rejected_jobs: list[RejectedJob] = []

    # ── 3. Phase 1 — Deduplication + offline pre-filter ──────────────────────
    pre_filtered: list[Job] = []

    for job in all_scraped_jobs:
        # Track duplicates (show them in dashboard, don't skip)
        is_duplicate  = job.job_hash in seen_jobs
        seen_jobs.add(job.job_hash)
        job.is_duplicate = is_duplicate

        passed_filter, reason, detail = apply_pre_filters(job, profile)
        if not passed_filter:
            rejected_jobs.append(_make_rejected(job, reason, detail))
        else:
            pre_filtered.append(job)

    logger.info(
        "Pre-filter: %d/%d jobs passed offline keyword/experience filters",
        len(pre_filtered), total_scraped,
    )

    # ── 4. Phase 2 — Offline ranking (fast, no API) ───────────────────────────
    if not pre_filtered:
        logger.warning("No jobs passed pre-filter — nothing to score.")
    else:
        ranked = offline_rank(pre_filtered, profile)

        # Top `limit_per_site` go to AI scoring; the rest are cut here
        ai_budget         = limit_per_site
        ai_candidates     = [job for job, _ in ranked[:ai_budget]]
        below_cutoff_jobs = [job for job, _ in ranked[ai_budget:]]

        if below_cutoff_jobs:
            logger.info(
                "📊 Offline rank cut: %d jobs below top-%d — skipping AI scoring for them",
                len(below_cutoff_jobs), ai_budget,
            )
        for job in below_cutoff_jobs:
            rejected_jobs.append(_make_rejected(
                job,
                "offline_rank_cut",
                f"Job ranked outside top-{ai_budget} offline candidates — not AI scored. "
                "Run with a higher --limit to include it.",
            ))

        logger.info(
            "🎯 AI scoring %d top-ranked candidates (budget: %d, available: %d)",
            len(ai_candidates), ai_budget, len(ranked),
        )

        # ── 5. Phase 3 — AI scoring ───────────────────────────────────────────
        quota_exhausted = False

        for idx, job in enumerate(ai_candidates, 1):
            # Progress heartbeat every 10 jobs
            if idx % 10 == 0 or idx == 1:
                logger.info(
                    "  Scoring job %d/%d: %s @ %s",
                    idx, len(ai_candidates), job.title, job.company,
                )

            if quota_exhausted:
                rejected_jobs.append(_make_rejected(
                    job,
                    "quota_skipped",
                    "Gemini API quota exhausted — score not calculated",
                ))
                continue

            try:
                ai_score = await scorer.score_job(job, profile)
            except QuotaExhaustedError:
                logger.warning("🚫 Gemini API limit reached — skipping AI scoring for remaining jobs")
                quota_exhausted = True
                rejected_jobs.append(_make_rejected(
                    job,
                    "quota_skipped",
                    "Gemini API quota exhausted — score not calculated",
                ))
                continue

            if ai_score is None:
                rejected_jobs.append(_make_rejected(
                    job,
                    "api_error",
                    "Gemini API failed to score this job",
                ))
                continue

            min_score = profile.job_preferences.min_ai_match_score
            if ai_score.score >= min_score:
                passed_jobs.append((job, ai_score))
            else:
                rejected_jobs.append(_make_rejected(
                    job,
                    "low_ai_score",
                    f"AI score {ai_score.score} < minimum threshold {min_score}",
                    ai_score.score,
                ))

    # ── 6. Save results ───────────────────────────────────────────────────────
    # Sort passed by AI score descending (best matches first)
    passed_jobs.sort(key=lambda x: x[1].score, reverse=True)

    passed_data = PassedRunData(
        sites_scraped=sites_scraped,
        limit_per_site=limit_per_site,
        total_scraped=total_scraped,
        total_failed=len(rejected_jobs),
        jobs=passed_jobs,
    )
    failed_data = FailedRunData(
        sites_scraped=sites_scraped,
        jobs=rejected_jobs,
    )

    save_passed(ts, passed_data)
    save_failed(ts, failed_data)

    # Persist caches only when they grew
    if len(seen_jobs) > initial_seen_count:
        save_seen_jobs(seen_jobs, len(seen_jobs) - initial_seen_count)
    if len(score_cache) > initial_cache_count:
        save_score_cache(score_cache, len(score_cache) - initial_cache_count)

    logger.info(
        "🎉 Pipeline complete! Passed: %d | AI-scored: %d | Pre-filter rejected: %d | Rank-cut: %d",
        len(passed_jobs),
        len([r for r in rejected_jobs if r.rejection_reason in ("low_ai_score", "api_error", "quota_skipped")]),
        len([r for r in rejected_jobs if r.rejection_reason not in ("low_ai_score", "api_error", "quota_skipped", "offline_rank_cut")]),
        len([r for r in rejected_jobs if r.rejection_reason == "offline_rank_cut"]),
    )
    return ts
