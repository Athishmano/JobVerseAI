"""
┌─ FILE: job_bot/schemas/profile.py
├─ PURPOSE: Pydantic v2 model that validates config/profile.json and strips all
│           _comment_* sibling keys before any field is touched.
├─ USED BY: core/storage.py (load_profile), core/ai_scorer.py (as_gemini_context)
├─ DATA FLOW: raw dict from profile.json → _strip_comments() → UserProfile
├─ DESIGN DECISIONS: _comment_ stripping is a model_validator(mode='before') so it
│                    runs before field assignment — no _comment_ key ever reaches
│                    a field validator or causes an 'extra field' error.
│                    as_gemini_context() lives here because UserProfile owns the
│                    data; ai_scorer.py is a consumer, not an owner.
│                    blacklist/whitelist lists are lowercased on load so every
│                    comparison at filter time is a simple 'in' check.
└─ PATTERNS: Pydantic v2 model_validator(mode='before'), nested models, Optional defaults
"""

from __future__ import annotations

from typing import Any, Optional

from pydantic import BaseModel, Field, field_validator, model_validator


# ── _comment_ key stripper ────────────────────────────────────────────────────

def _strip_comments(data: Any) -> Any:
    """
    Recursively remove every key that starts with '_comment_' from dicts.

    Handles arbitrary nesting so that _comment_ keys inside job_preferences,
    individual experience entries, etc. are all stripped regardless of depth.
    """
    if isinstance(data, dict):
        return {
            k: _strip_comments(v)
            for k, v in data.items()
            if not k.startswith("_comment_")
        }
    if isinstance(data, list):
        return [_strip_comments(item) for item in data]
    return data


# ── Sub-models ────────────────────────────────────────────────────────────────

class PersonalInfo(BaseModel):
    name: str
    email: str
    phone: str
    location: str


class Experience(BaseModel):
    company: str
    role: str
    start_date: str   # "YYYY-MM"
    end_date: str     # "YYYY-MM" or "present"
    bullets: list[str] = Field(default_factory=list)


class Education(BaseModel):
    institution: str
    degree: str
    field: str
    year: int


class Hackathon(BaseModel):
    name: str
    result: str
    date: str
    description: str


class JobPreferences(BaseModel):
    titles: list[str] = Field(..., min_length=1)
    locations: list[str] = Field(..., min_length=1)
    remote_ok: bool = True
    min_salary: Optional[int] = None
    max_experience_years: int = Field(default=99, ge=0)
    blacklist_companies: list[str] = Field(default_factory=list)
    blacklist_keywords: list[str] = Field(default_factory=list)
    whitelist_keywords: list[str] = Field(default_factory=list)
    min_ai_match_score: int = Field(default=40, ge=0, le=100)

    @field_validator(
        "blacklist_companies",
        "blacklist_keywords",
        "whitelist_keywords",
        mode="after",
    )
    @classmethod
    def _lowercase(cls, v: list[str]) -> list[str]:
        """
        Normalise all keyword/company lists to lowercase + stripped at load time.
        This means every comparison in filters.py is a simple case-insensitive
        substring check without repeated .lower() calls in the hot path.
        """
        return [item.lower().strip() for item in v]


class ScreeningAnswers(BaseModel):
    years_of_experience: str = "0"
    current_ctc: str = "Fresher"
    expected_ctc: str = "Negotiable"
    notice_period: str = "Immediate"
    willing_to_relocate: bool = True
    work_authorization: str = "Yes, authorized to work"
    sponsorship_required: bool = False


# ── Root model ────────────────────────────────────────────────────────────────

class UserProfile(BaseModel):
    """
    Fully validated representation of config/profile.json.

    All _comment_* sibling keys are stripped before any field is validated,
    so the template can contain as many instructional comments as needed without
    breaking schema validation.
    """

    personal: PersonalInfo
    resume_pdf_path: str = "./config/resume.pdf"
    summary: str
    experience: list[Experience] = Field(default_factory=list)
    education: list[Education] = Field(default_factory=list)
    skills: list[str] = Field(..., min_length=1)
    certifications: list[str] = Field(default_factory=list)
    hackathons: list[Hackathon] = Field(default_factory=list)
    job_preferences: JobPreferences
    screening_answers: ScreeningAnswers = Field(default_factory=ScreeningAnswers)

    @model_validator(mode="before")
    @classmethod
    def _strip_comment_keys(cls, data: Any) -> Any:
        """Strip all _comment_* keys recursively before Pydantic validates fields."""
        return _strip_comments(data)

    # ── Gemini context builder ────────────────────────────────────────────────

    def as_gemini_context(self) -> str:
        """
        Render the profile as a structured plain-text block for inclusion in the
        Gemini scoring prompt.

        Called by ai_scorer.py for every job that reaches the AI stage.
        Returns a human-readable, section-headed string that gives Gemini
        sufficient context to produce a high-quality match score.

        Example output (truncated):
            Candidate: Gurusabarivasan M
            Location: Chennai, Tamil Nadu, India

            ## Professional Summary
            Full-stack developer with 1 year of internship experience ...

            ## Skills
            React, Node.js, Python, PostgreSQL, Docker

            ## Experience
            - Frontend Intern at Acme Corp (2024-06 → 2025-01)
              • Built a dashboard reducing report generation time by 60%
        """
        lines: list[str] = [
            f"Candidate: {self.personal.name}",
            f"Location: {self.personal.location}",
        ]

        lines += [
            "",
            "## Professional Summary",
            self.summary,
        ]

        lines += [
            "",
            "## Skills",
            ", ".join(self.skills),
        ]

        if self.certifications:
            lines += [
                "",
                "## Certifications",
                ", ".join(self.certifications),
            ]

        if self.experience:
            lines += ["", "## Work Experience"]
            for exp in self.experience:
                lines.append(
                    f"- {exp.role} at {exp.company}"
                    f" ({exp.start_date} → {exp.end_date})"
                )
                for bullet in exp.bullets:
                    lines.append(f"  • {bullet}")

        if self.education:
            lines += ["", "## Education"]
            for edu in self.education:
                lines.append(
                    f"- {edu.degree} in {edu.field},"
                    f" {edu.institution} ({edu.year})"
                )

        if self.hackathons:
            lines += ["", "## Hackathons & Projects"]
            for h in self.hackathons:
                lines.append(
                    f"- {h.name} ({h.result}, {h.date}): {h.description}"
                )

        return "\n".join(lines)
