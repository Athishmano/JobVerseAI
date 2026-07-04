"""
┌─ FILE: job_bot/schemas/job.py
├─ PURPOSE: Pydantic v2 models for the canonical Job, AIScore, and RejectedJob
│           data structures shared across every pipeline stage.
├─ USED BY: scrapers/*.py (produce Job), core/filters.py (produces RejectedJob),
│           core/ai_scorer.py (produces AIScore + RejectedJob on api_error),
│           core/storage.py (serialises all three), api/routes/results.py
├─ DATA FLOW: scraper → Job → filters → (RejectedJob | Job) → ai_scorer →
│             (AIScore attached to Job | RejectedJob) → storage
├─ DESIGN DECISIONS: applicant_count is Optional[int] with NO default so the type
│                    system forces every scraper to explicitly pass None when the
│                    portal does not expose a count — it can never silently be 0.
│                    job_hash is a @computed_field so it is always consistent with
│                    the current field values and automatically appears in model_dump().
│                    RejectedJob extends Job (not a separate model) so it carries the
│                    full job data, enabling the Failed file to show full context.
└─ PATTERNS: Pydantic v2 computed_field, Literal union for enums, strict ge/le bounds
"""

import hashlib
from typing import Literal, Optional

from pydantic import BaseModel, Field, computed_field


class Job(BaseModel):
    """
    Normalised job posting — the canonical output of every scraper.

    All scrapers must produce this exact shape regardless of portal.
    Fields that a portal does not expose must be set to None explicitly.
    """

    source: str = Field(
        ...,
        description="Portal name: 'naukri' | 'linkedin' | 'indeed' | 'wellfound'",
    )
    job_id: str = Field(..., description="Portal-native unique job identifier")
    title: str
    company: str
    location: str
    salary: Optional[str] = None
    employment_type: Optional[str] = None        # "Full-time", "Contract", "Internship", etc.
    experience_required: Optional[str] = None    # raw portal string, e.g. "0–1 years", "Fresher"
    description: str
    apply_url: str
    posted_date: Optional[str] = None            # raw portal string — no normalisation here
    skills_required: list[str] = Field(default_factory=list)

    is_duplicate: bool = Field(
        default=False,
        description="Whether this job has been seen in a previous run."
    )

    # HARD RULE: NEVER default to 0.  None means "portal did not expose this value".
    applicant_count: Optional[int] = Field(
        default=None,
        description="Number of applicants. None when not exposed by the portal.",
    )

    @computed_field  # type: ignore[misc]
    @property
    def job_hash(self) -> str:
        """
        SHA-256 of (title + company + description), normalised to lowercase + stripped.

        This hash is:
        - The dedup key stored in data/seen_jobs.json (set of hashes across all runs)
        - The cache key in data/score_cache.json (maps hash → AIScore)

        Identical postings from different portals or repeated across runs produce the
        same hash, ensuring they are scraped but never re-scored.
        """
        raw = (
            self.title.lower().strip()
            + self.company.lower().strip()
            + self.description.lower().strip()
        )
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()


class AIScore(BaseModel):
    """
    Structured Gemini scoring response.

    ai_scorer.py instructs Gemini to return JSON matching this schema exactly,
    then validates it here with Pydantic.  On malformed JSON the scorer retries
    up to 3x before giving up and producing a RejectedJob(api_error).
    """

    score: int = Field(..., ge=0, le=100, description="Match quality 0–100")
    reason: str = Field(
        ...,
        description="1–3 sentence summary of why the candidate is or isn't a fit",
    )
    strengths: list[str] = Field(
        ...,
        description="Candidate's matching strengths relative to this JD",
    )
    missing_skills: list[str] = Field(
        ...,
        description="Skills mentioned in the JD that the candidate lacks",
    )
    recommendation: Literal["Apply", "Consider", "Skip"]
    improvement_tips: str = Field(
        ...,
        description="Company-specific advice on what to improve before applying",
    )


class RejectedJob(Job):
    """
    A Job that did not make it to the Passed file.

    Rejection can happen at three points in the pipeline:
      1. Pre-AI filter  → blacklist_keyword | blacklist_company | max_experience_exceeded
      2. AI scoring     → low_ai_score (scored < min_ai_match_score)
      3. Gemini failure → api_error (all retries exhausted)

    ai_score is None for cases 1 and 3.
    """

    rejection_reason: Literal[
        "blacklist_keyword",
        "blacklist_company",
        "max_experience_exceeded",
        "low_ai_score",
        "api_error",
        "quota_skipped",
        "offline_rank_cut",
    ]
    rejection_detail: str = Field(
        ...,
        description=(
            "Human-readable explanation, e.g. "
            "\"Title contains blacklisted keyword: 'staff engineer'\""
        ),
    )
    ai_score: Optional[int] = Field(
        default=None,
        ge=0,
        le=100,
        description="None when rejected before AI scoring or on API failure",
    )
