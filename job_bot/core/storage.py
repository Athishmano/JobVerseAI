"""
┌─ FILE: job_bot/core/storage.py
├─ PURPOSE: Single source of truth for ALL file I/O in the project.
│           Every read from / write to disk goes through this module —
│           no other module should call open() or Path.read_text() directly.
├─ USED BY: services/pipeline.py (load profile, save results, update seen/cache),
│           core/ai_scorer.py (load/save score cache),
│           api/routes/results.py (list_runs, get_run)
├─ DATA FLOW:
│   load_profile()       config/profile.json       → UserProfile
│   load_seen_jobs()     data/seen_jobs.json        → set[job_hash]
│   save_seen_jobs()     set[job_hash]              → data/seen_jobs.json
│   load_score_cache()   data/score_cache.json      → dict[hash, AIScore]
│   save_score_cache()   dict[hash, AIScore]        → data/score_cache.json
│   save_passed()        PassedRunData              → results/Passed/<ts>.json
│   save_failed()        FailedRunData              → results/Failed/<ts>.json
│   list_runs()          results/Passed/ dir scan   → list[timestamp str]
│   get_run(ts)          results/Passed+Failed dirs → combined dict
├─ DESIGN DECISIONS: All paths are computed relative to __file__ (not cwd) so the
│                    CLI can be run from any directory. _read_json / _write_json are
│                    private helpers so callers never deal with raw open() / json.*.
│                    PassedRunData / FailedRunData are dataclasses (not Pydantic) —
│                    they are internal pipeline containers, not user-facing schemas.
└─ PATTERNS: Centralised I/O, fail-fast on profile errors, graceful degradation on
             corrupt cache entries, 💾 log on every write
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

from pydantic import ValidationError

from job_bot.core.logger import log_error, log_warning
from job_bot.schemas.job import AIScore, Job, RejectedJob
from job_bot.schemas.profile import UserProfile

logger = logging.getLogger(__name__)

# ── Absolute paths ─────────────────────────────────────────────────────────────
# storage.py lives at  job_bot/core/storage.py
# parents[0] = job_bot/core/   parents[1] = job_bot/   parents[2] = project root
_ROOT = Path(__file__).resolve().parents[2]

PROFILE_PATH       = _ROOT / "config" / "profile.json"
SEEN_JOBS_PATH     = _ROOT / "data"   / "seen_jobs.json"
SCORE_CACHE_PATH   = _ROOT / "data"   / "score_cache.json"
RESULTS_PASSED_DIR = _ROOT / "results" / "Passed"
RESULTS_FAILED_DIR = _ROOT / "results" / "Failed"


# ── Run data containers ────────────────────────────────────────────────────────

@dataclass
class PassedRunData:
    """
    Carries everything pipeline.py needs to call save_passed().

    ``jobs`` is a list of (Job, AIScore) pairs — jobs that scored >=
    min_ai_match_score.  total_failed is tracked separately because it
    includes pre-filter rejections that are not in the Failed job list
    (they live in FailedRunData.jobs).
    """
    sites_scraped:  list[str]
    limit_per_site: int
    total_scraped:  int
    total_failed:   int
    jobs: list[tuple[Job, AIScore]] = field(default_factory=list)

    @property
    def total_passed(self) -> int:
        return len(self.jobs)


@dataclass
class FailedRunData:
    """
    Carries everything pipeline.py needs to call save_failed().

    ``jobs`` includes ALL rejected jobs regardless of rejection stage:
    pre-filter rejections, low AI score, and api_error.
    """
    sites_scraped: list[str]
    jobs: list[RejectedJob] = field(default_factory=list)

    @property
    def total_failed(self) -> int:
        return len(self.jobs)


# ── Timestamp utilities ────────────────────────────────────────────────────────

def generate_run_timestamp() -> str:
    """
    Generate a filesystem-safe run timestamp string.

    Format  : dd-mm-yyyy_HH-MM-SS   (hyphens only — colons are illegal in Windows paths)
    Example : 03-01-2026_13-30-00

    This string is used as:
    - The filename stem:  results/Passed/03-01-2026_13-30-00.json
    - The query parameter: /jobs?run=03-01-2026_13-30-00
    - The key passed to   get_run(timestamp)
    """
    return datetime.now().strftime("%d-%m-%Y_%H-%M-%S")


def _ts_to_readable(ts: str) -> str:
    """
    Convert filename timestamp → human-readable timestamp for the JSON ``timestamp`` field.

    '03-01-2026_13-30-00'  →  '03-01-2026/13:30:00'
    """
    date_part, time_part = ts.split("_", 1)
    return f"{date_part}/{time_part.replace('-', ':')}"


# ── Low-level I/O helpers ──────────────────────────────────────────────────────

def _read_json(path: Path, default: Any) -> Any:
    """
    Read and parse a JSON file.  Returns *default* if the file does not exist
    or is not valid JSON — never raises for these cases so callers can proceed
    with sensible defaults.
    """
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        log_warning(
            logger, "storage.py", "_read_json",
            f"Could not read {path.name}: {exc}",
            "using default value",
        )
        return default


def _write_json(path: Path, data: Any) -> None:
    """Write *data* as pretty-printed JSON, creating parent directories as needed."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(data, ensure_ascii=False, indent=2, default=str),
        encoding="utf-8",
    )


# ── Public API — Profile ───────────────────────────────────────────────────────

def load_profile() -> UserProfile:
    """
    Load, parse, and validate config/profile.json.

    Behaviour:
    - All _comment_* keys are stripped automatically by UserProfile's model_validator.
    - If resume_pdf_path points to a non-existent file: logs WARNING, run continues
      (upload steps will skip gracefully).
    - Fatal SystemExit(1) on: file not found, invalid JSON, schema validation failure.

    Logs:
        14:14:00 | INFO     | Loaded resume for Gurusabarivasan M
        14:14:00 | WARNING  | resume_pdf_path './config/resume.pdf' does not exist; using default
    """
    # ── Read ──────────────────────────────────────────────────────────────────
    try:
        raw: dict[str, Any] = json.loads(PROFILE_PATH.read_text(encoding="utf-8"))
    except FileNotFoundError:
        log_error(
            logger, "storage.py", "load_profile",
            f"config/profile.json not found at {PROFILE_PATH}",
            "cannot continue",
        )
        raise SystemExit(1)
    except json.JSONDecodeError as exc:
        log_error(
            logger, "storage.py", "load_profile",
            f"config/profile.json is not valid JSON: {exc}",
            "cannot continue",
        )
        raise SystemExit(1)

    # ── Validate ──────────────────────────────────────────────────────────────
    try:
        profile = UserProfile.model_validate(raw)
    except ValidationError as exc:
        log_error(
            logger, "storage.py", "load_profile",
            f"config/profile.json failed schema validation: {exc}",
            "cannot continue",
        )
        raise SystemExit(1)

    # ── Resume PDF check ──────────────────────────────────────────────────────
    resume_path = _ROOT / profile.resume_pdf_path.lstrip("./")
    if not resume_path.exists():
        log_warning(
            logger, "storage.py", "load_profile",
            f"resume_pdf_path '{profile.resume_pdf_path}' does not exist",
            "upload steps will be skipped for this run",
        )

    logger.info("Loaded resume for %s", profile.personal.name)
    return profile


# ── Public API — Seen Jobs (dedup index) ───────────────────────────────────────

def load_seen_jobs() -> set[str]:
    """
    Load data/seen_jobs.json → set of job_hash strings.

    The set is used by pipeline.py to skip already-scraped jobs before
    feeding them into the filter/score pipeline.  Returns an empty set if
    the file doesn't exist yet (first run).
    """
    data: list[str] = _read_json(SEEN_JOBS_PATH, default=[])
    return set(data)


def save_seen_jobs(seen: set[str], new_count: int = 0) -> None:
    """
    Persist the updated dedup index to data/seen_jobs.json.

    Stores as a sorted list (deterministic diffs in git).

    Logs:
        14:22:00 | INFO | 💾 Updated data/seen_jobs.json (+150 entries)
    """
    _write_json(SEEN_JOBS_PATH, sorted(seen))
    logger.info(
        "💾 Updated data/seen_jobs.json (+%d entries)", new_count or len(seen)
    )


# ── Public API — Score Cache ───────────────────────────────────────────────────

def load_score_cache() -> dict[str, AIScore]:
    """
    Load data/score_cache.json → dict[job_hash → AIScore].

    Silently skips any cache entries that fail AIScore validation (a corrupt
    entry causes a warning and is re-scored on the next run rather than
    crashing the whole pipeline).
    """
    raw: dict[str, Any] = _read_json(SCORE_CACHE_PATH, default={})
    cache: dict[str, AIScore] = {}
    skipped = 0

    for job_hash, score_data in raw.items():
        try:
            cache[job_hash] = AIScore.model_validate(score_data)
        except (ValidationError, TypeError, AttributeError):
            skipped += 1

    if skipped:
        log_warning(
            logger, "storage.py", "load_score_cache",
            f"{skipped} corrupt cache entr{'y' if skipped == 1 else 'ies'} skipped",
            "affected jobs will be re-scored",
        )

    return cache


def save_score_cache(cache: dict[str, AIScore], new_count: int = 0) -> None:
    """
    Persist the score cache to data/score_cache.json.

    Logs:
        14:22:00 | INFO | 💾 Updated data/score_cache.json (+52 entries)
    """
    raw = {job_hash: score.model_dump() for job_hash, score in cache.items()}
    _write_json(SCORE_CACHE_PATH, raw)
    logger.info(
        "💾 Updated data/score_cache.json (+%d entries)", new_count or len(cache)
    )


# ── Public API — Results ───────────────────────────────────────────────────────

def save_passed(timestamp: str, run_data: PassedRunData) -> None:
    """
    Serialise and write results/Passed/<timestamp>.json.

    Output shape (per master prompt spec):
    {
      "timestamp":      "dd-mm-yyyy/HH:MM:SS",
      "sites_scraped":  [...],
      "limit_per_site": N,
      "total_scraped":  N,
      "total_passed":   N,
      "total_failed":   N,
      "best_matches":   [{ full job + AI score fields }]
    }

    The ``apply_url`` field is serialised as ``url`` to match the spec.

    Logs:
        14:22:00 | INFO | 💾 Saved results/Passed/03-01-2026_13-30-00.json
    """
    best_matches: list[dict[str, Any]] = []
    for job, score in run_data.jobs:
        best_matches.append({
            "source":              job.source,
            "job_id":              job.job_id,
            "title":               job.title,
            "company":             job.company,
            "location":            job.location,
            "salary":              job.salary,
            "url":                 job.apply_url,   # renamed per spec
            "description":         job.description,
            "posted_date":         job.posted_date,
            "employment_type":     job.employment_type,
            "experience_required": job.experience_required,
            "skills_required":     job.skills_required,
            "applicant_count":     job.applicant_count,
            "is_duplicate":        job.is_duplicate,
            # AI scoring fields
            "ai_score":            score.score,
            "reasoning":           score.reason,
            "strengths":           score.strengths,
            "missing_skills":      score.missing_skills,
            "recommendation":      score.recommendation,
            "improvement_tips":    score.improvement_tips,
        })

    output: dict[str, Any] = {
        "timestamp":      _ts_to_readable(timestamp),
        "sites_scraped":  run_data.sites_scraped,
        "limit_per_site": run_data.limit_per_site,
        "total_scraped":  run_data.total_scraped,
        "total_passed":   run_data.total_passed,
        "total_failed":   run_data.total_failed,
        "best_matches":   best_matches,
    }

    path = RESULTS_PASSED_DIR / f"{timestamp}.json"
    _write_json(path, output)
    logger.info("💾 Saved results/Passed/%s.json", timestamp)


def save_failed(timestamp: str, run_data: FailedRunData) -> None:
    """
    Serialise and write results/Failed/<timestamp>.json.

    Output shape (per master prompt spec):
    {
      "timestamp":     "dd-mm-yyyy/HH:MM:SS",
      "sites_scraped": [...],
      "total_failed":  N,
      "rejected_jobs": [{ source, job_id, title, company, location, url,
                          applicant_count, rejection_reason, rejection_detail,
                          ai_score }]
    }

    Logs:
        14:22:00 | INFO | 💾 Saved results/Failed/03-01-2026_13-30-00.json
    """
    rejected: list[dict[str, Any]] = []
    for job in run_data.jobs:
        rejected.append({
            "source":           job.source,
            "job_id":           job.job_id,
            "title":            job.title,
            "company":          job.company,
            "location":         job.location,
            "url":              job.apply_url,
            "applicant_count":  job.applicant_count,
            "is_duplicate":     job.is_duplicate,
            "rejection_reason": job.rejection_reason,
            "rejection_detail": job.rejection_detail,
            "ai_score":         job.ai_score,
        })

    output: dict[str, Any] = {
        "timestamp":     _ts_to_readable(timestamp),
        "sites_scraped": run_data.sites_scraped,
        "total_failed":  run_data.total_failed,
        "rejected_jobs": rejected,
    }

    path = RESULTS_FAILED_DIR / f"{timestamp}.json"
    _write_json(path, output)
    logger.info("💾 Saved results/Failed/%s.json", timestamp)


# ── Public API — Run History ───────────────────────────────────────────────────

def list_runs() -> list[str]:
    """
    Scan results/Passed/ and return all run timestamps, newest first.

    Returns an empty list if no runs have completed yet (first-time setup or
    empty results directory).  The frontend /history page calls this via
    GET /api/v1/results.

    Return value: list of timestamp strings in 'dd-mm-yyyy_HH-MM-SS' format.
    """
    if not RESULTS_PASSED_DIR.exists():
        return []

    timestamps = [
        p.stem
        for p in RESULTS_PASSED_DIR.iterdir()
        if p.suffix == ".json" and not p.stem.startswith(".")
    ]

    # Sort descending by parsing into datetime objects for correctness across years
    def _parse_ts(ts: str) -> datetime:
        try:
            return datetime.strptime(ts, "%d-%m-%Y_%H-%M-%S")
        except ValueError:
            return datetime.min  # push malformed entries to the end

    return sorted(timestamps, key=_parse_ts, reverse=True)


def get_run(timestamp: str) -> dict[str, Any]:
    """
    Load both Passed and Failed files for a given run timestamp and return them
    as a combined dict.  Called by the FastAPI results endpoint.

    Return shape:
    {
        "timestamp": "<ts>",
        "passed":    { ...full Passed file content... },
        "failed":    { ...full Failed file content... },
    }

    Raises:
        FileNotFoundError: if no Passed file exists for this timestamp.
    """
    passed_path = RESULTS_PASSED_DIR / f"{timestamp}.json"
    failed_path = RESULTS_FAILED_DIR / f"{timestamp}.json"

    if not passed_path.exists():
        raise FileNotFoundError(
            f"No Passed results file for timestamp '{timestamp}' "
            f"(expected at {passed_path})"
        )

    passed_data = _read_json(passed_path, default={})
    failed_data = _read_json(
        failed_path,
        default={"rejected_jobs": [], "total_failed": 0},
    )

    return {
        "timestamp": timestamp,
        "passed":    passed_data,
        "failed":    failed_data,
    }
