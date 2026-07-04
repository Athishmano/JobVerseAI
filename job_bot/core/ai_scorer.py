"""
┌─ FILE: job_bot/core/ai_scorer.py
├─ PURPOSE: Interfaces with the Gemini API to evaluate jobs against the user's
│           profile. Handles prompt construction, structured output parsing,
│           and caching logic.
├─ USED BY: services/pipeline.py
├─ DATA FLOW: Job + UserProfile → prompt → Gemini API → AIScore
├─ DESIGN DECISIONS: Uses gemini-2.5-flash-lite for speed/cost. Enforces JSON output
│                    matching the AIScore schema using response_schema.
└─ PATTERNS: Async API calls, retries on 429/500, prompt templating.
"""

import asyncio
import json
import logging
import re
import time
from typing import Optional

from google import genai
from google.genai import types

from job_bot.core.config import Settings
from job_bot.core.logger import log_error, log_warning
from job_bot.schemas.job import AIScore, Job
from job_bot.schemas.profile import UserProfile

logger = logging.getLogger(__name__)

# System instructions to guide Gemini's scoring logic
_SYSTEM_INSTRUCTION = """
You are an expert technical recruiter and career coach.
Your task is to evaluate a job description against a candidate's profile and provide a structured JSON assessment.

Score the match from 0 to 100 based on:
1. Skill overlap (most important)
2. Experience level match (e.g., don't recommend senior roles to freshers)
3. Location / Remote preferences
4. Salary expectations (if available)

Provide a brief, honest reason for the score. List key strengths and missing skills.
Recommendation must be exactly one of: 'Apply', 'Skip', 'Reach out to recruiter'.
Provide one concrete improvement tip for the candidate.
"""

class QuotaExhaustedError(Exception):
    """Raised when the Gemini API quota limit is exhausted (429/Resource Exhausted)."""
    pass

class AIScorer:
    """
    Service class for interacting with the Gemini API.

    Model: gemini-2.0-flash-lite
      Free tier limits: 30 RPM, 1 500 RPD, 1 000 000 TPM.
      This gives 75× more daily headroom than gemini-2.5-flash-lite (20 RPD).

    Rate limiter: enforces _MIN_REQUEST_INTERVAL seconds between calls to stay
    safely under the 30 RPM cap without needing to react to 429 errors.
    """

    # gemini-2.0-flash-lite: 30 RPM → 1 call per 2s fits comfortably
    _MODEL = "gemini-2.0-flash-lite"
    _MIN_REQUEST_INTERVAL = 2.5  # seconds (fits ~24 RPM — safe buffer below 30)

    def __init__(self, settings: Settings, score_cache: dict[str, AIScore]):
        self.settings = settings
        self.cache = score_cache
        # Ensure the API key is present
        api_key = self.settings.gemini_api_key if self.settings.gemini_api_key else None
        if not api_key:
            raise ValueError("GEMINI_API_KEY is not set in environment or config")
        
        self.client = genai.Client(api_key=api_key)
        self._last_request_time: float = 0.0  # timestamp of last Gemini call

    async def score_job(self, job: Job, profile: UserProfile) -> Optional[AIScore]:
        """
        Evaluate a single job against the profile.
        Checks the cache first based on job.job_hash.
        Returns None if the API fails permanently.
        """
        if job.job_hash in self.cache:
            # Cache hit
            return self.cache[job.job_hash]

        prompt = self._build_prompt(job, profile)
        
        # We need to run the synchronous client in a thread pool since the new
        # google-genai client's async support may require the async client initialization.
        # For simplicity and robust async I/O without blocking the event loop:
        loop = asyncio.get_running_loop()

        max_retries = 3
        base_delay = 2.0

        for attempt in range(1, max_retries + 1):
            try:
                # Rate limiter: enforce minimum interval between requests
                now = time.monotonic()
                elapsed = now - self._last_request_time
                if elapsed < self._MIN_REQUEST_INTERVAL:
                    wait = self._MIN_REQUEST_INTERVAL - elapsed
                    logger.debug("Rate limiter: waiting %.1fs before next Gemini call", wait)
                    await asyncio.sleep(wait)

                self._last_request_time = time.monotonic()

                # Use generate_content synchronously but offload to executor
                response = await loop.run_in_executor(
                    None,
                    self._call_gemini,
                    prompt
                )

                # Parse the output
                text_content = response.text
                if not text_content:
                    raise ValueError("Empty response from Gemini")

                # The response is guaranteed to be JSON matching AIScore schema
                parsed_dict = json.loads(text_content)
                score = AIScore.model_validate(parsed_dict)

                # Update cache
                self.cache[job.job_hash] = score
                return score

            except Exception as e:
                err_str = str(e).lower()
                is_rate_limit   = "429" in err_str or "resource_exhausted" in err_str or "quota" in err_str
                is_daily_limit  = "per_day" in err_str or "perday" in err_str or "GenerateRequestsPerDay".lower() in err_str
                is_server_error = "503" in err_str or "unavailable" in err_str or "500" in err_str

                # If the daily RPD cap is hit, there is nothing to retry — stop immediately
                if is_daily_limit:
                    raise QuotaExhaustedError(f"Gemini daily quota exhausted: {e}")

                if attempt == max_retries:
                    if is_rate_limit:
                        raise QuotaExhaustedError(f"Gemini API rate limit reached: {e}")
                    log_error(
                        logger, "ai_scorer.py", "score_job",
                        f"Failed to score job {job.job_id} after {max_retries} attempts: {e}",
                        "returning None (job will be skipped)",
                    )
                    return None

                # Choose delay strategy based on error type:
                if is_rate_limit:
                    # Try to parse the API's own retry-after hint  e.g. "retry in 37.08s"
                    retry_match = re.search(r"retry[^0-9]*([0-9]+(?:\.[0-9]+)?)", str(e), re.IGNORECASE)
                    delay = float(retry_match.group(1)) + 2.0 if retry_match else base_delay * (2 ** attempt)
                elif is_server_error:
                    # Model overloaded — give it more breathing room
                    delay = 10.0 * attempt
                else:
                    delay = base_delay * (2 ** (attempt - 1))

                log_warning(
                    logger, "ai_scorer.py", "score_job",
                    f"Gemini API error (attempt {attempt}/{max_retries}): {e}",
                    f"retrying in {delay:.0f}s",
                )
                await asyncio.sleep(delay)
                
        return None

    def _call_gemini(self, prompt: str):
        """Synchronous API call wrapped for the executor."""
        return self.client.models.generate_content(
            model=self._MODEL,
            contents=prompt,
            config=types.GenerateContentConfig(
                response_mime_type="application/json",
                response_schema=AIScore,
                system_instruction=_SYSTEM_INSTRUCTION,
                temperature=0.2,  # Low temperature for analytical/consistent scoring
            ),
        )

    def _build_prompt(self, job: Job, profile: UserProfile) -> str:
        """
        Constructs the text prompt comparing the candidate and the job.
        """
        profile_context = profile.as_gemini_context()
        
        prompt = f"""
Please evaluate the following job opportunity against the candidate's profile.

==================================================
CANDIDATE PROFILE:
==================================================
{profile_context}

==================================================
JOB OPPORTUNITY:
==================================================
Title: {job.title}
Company: {job.company}
Location: {job.location}
Salary: {job.salary or 'Not specified'}
Experience Required: {job.experience_required or 'Not specified'}

Job Description:
{job.description}
==================================================

Analyze the match and provide the JSON assessment.
"""
        return prompt
