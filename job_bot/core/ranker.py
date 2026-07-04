"""
┌─ FILE: job_bot/core/ranker.py
├─ PURPOSE: Fast, offline keyword-based scoring to pre-rank scraped jobs
│           before the expensive Gemini AI scoring step.
├─ USED BY: services/pipeline.py (after pre-filter, before AI scoring)
├─ DATA FLOW: list[Job] + UserProfile → offline_rank() → list[(Job, float)]
├─ DESIGN DECISIONS: Entirely offline — no API calls, no I/O.
│                    Runs in milliseconds even for 500+ jobs.
│                    Score components:
│                      40 pts — title keyword overlap with profile search titles
│                      40 pts — skill keyword presence in job text (4 pts each)
│                      20 pts — whitelist keyword bonus (10 pts each, capped)
└─ PATTERNS: Pure function, simple scoring heuristic.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from job_bot.schemas.job import Job
    from job_bot.schemas.profile import UserProfile

logger = logging.getLogger(__name__)


def offline_rank(jobs: list["Job"], profile: "UserProfile") -> list[tuple["Job", float]]:
    """
    Score and rank jobs by offline keyword matching against the user profile.

    Returns a list of (Job, score) tuples sorted by score descending.
    The score is purely indicative — the AI scorer produces the authoritative score.

    Args:
        jobs:    Pre-filtered list of Job objects ready for ranking.
        profile: Loaded UserProfile (skills, titles, whitelist keywords).

    Returns:
        Sorted list of (job, offline_score) pairs, highest first.
    """
    if not jobs:
        return []

    # Pre-compute keyword sets once (all lowercased by schema validators already)
    profile_title_keywords: set[str] = set()
    for t in profile.job_preferences.titles:
        profile_title_keywords.update(t.lower().split())

    profile_skills_lower: set[str] = {s.lower() for s in profile.skills}
    whitelist: set[str] = set(profile.job_preferences.whitelist_keywords)  # already lowercased

    scored: list[tuple[Job, float]] = []

    for job in jobs:
        score = 0.0
        title_lower = (job.title or "").lower()
        desc_lower  = (job.description or "").lower()
        full_text   = title_lower + " " + desc_lower

        # ── 1. Title keyword overlap (0–40 pts) ──────────────────────────────
        title_words = set(title_lower.split())
        overlap     = title_words & profile_title_keywords
        score += min(len(overlap) * 10.0, 40.0)

        # ── 2. Skill keyword hits in full text (0–40 pts, 4 pts per skill) ──
        skill_hits = sum(1 for skill in profile_skills_lower if skill in full_text)
        score += min(skill_hits * 4.0, 40.0)

        # ── 3. Whitelist keyword bonus (0–20 pts, 10 pts each) ───────────────
        wl_hits = sum(1 for kw in whitelist if kw in full_text)
        score += min(wl_hits * 10.0, 20.0)

        scored.append((job, score))

    scored.sort(key=lambda x: x[1], reverse=True)

    logger.debug(
        "Offline ranked %d jobs — top score: %.0f, median: %.0f",
        len(scored),
        scored[0][1] if scored else 0,
        scored[len(scored) // 2][1] if scored else 0,
    )

    return scored
