"""
┌─ FILE: job_bot/core/filter.py
├─ PURPOSE: Offline heuristics for fast-path rejection. Evaluates scraped jobs
│           against user preferences before sending them to the expensive AI layer.
├─ USED BY: services/pipeline.py
├─ DATA FLOW: Job + UserProfile → apply_pre_filters() → tuple[passed, reason, detail]
├─ DESIGN DECISIONS: Fast, synchronous text matching. Rejections skip the Gemini API,
│                    saving time and tokens.
└─ PATTERNS: Pure function, fail-fast sequence.
"""

from typing import Tuple

from job_bot.schemas.job import Job
from job_bot.schemas.profile import UserProfile


def apply_pre_filters(job: Job, profile: UserProfile) -> Tuple[bool, str, str]:
    """
    Evaluate a job against the strict inclusion/exclusion rules in the user profile.
    
    This runs entirely offline to save Gemini API costs. The first rule that
    fails triggers immediate rejection.
    
    Returns:
        (passed, rejection_reason, rejection_detail)
        
        If passed == True, the strings are empty.
        If passed == False, reason is a categorical code (e.g., 'blacklist_keyword')
        and detail is a human-readable explanation (e.g., "Contains 'unpaid'").
    """
    prefs = profile.job_preferences

    # 1. Blacklist companies (exact match or substring)
    if prefs.blacklist_companies and job.company:
        company_lower = job.company.lower()
        for bad_company in prefs.blacklist_companies:
            if bad_company in company_lower:
                return (
                    False,
                    "blacklist_company",
                    f"Matched blacklisted company: '{bad_company}'"
                )

    # 2. Blacklist keywords (case-insensitive search across title, company, desc)
    if prefs.blacklist_keywords:
        # Pre-process text to lower case once
        text_corpus = " ".join(filter(None, [job.title, job.company, job.description])).lower()
        
        for keyword in prefs.blacklist_keywords:
            # We assume keywords in profile are already lowered (done in schema validator)
            if keyword in text_corpus:
                return (
                    False,
                    "blacklist_keyword",
                    f"Matched blacklisted keyword: '{keyword}'"
                )

    return (True, "", "")
