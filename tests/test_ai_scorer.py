import pytest
from unittest.mock import MagicMock
from job_bot.core.ai_scorer import AIScorer
from job_bot.schemas.job import Job
from job_bot.schemas.profile import UserProfile
from job_bot.core.config import Settings

@pytest.fixture
def mock_scorer():
    settings = Settings(gemini_api_key="fake-key")
    scorer = AIScorer(settings=settings, score_cache={})
    scorer._call_gemini = MagicMock()
    return scorer

@pytest.mark.asyncio
async def test_ai_scorer_caching(mock_scorer):
    profile = UserProfile.model_validate({
        "personal": {"name": "Test User", "email": "test@test.com", "phone": "123", "location": "NY"},
        "summary": "Dev",
        "skills": ["Python"],
        "job_preferences": {"titles": ["Dev"], "locations": ["Remote"]}
    })
    
    job = Job(
        source="test", job_id="1", title="Dev", company="Acme",
        location="Remote", description="Great job", apply_url="http://a"
    )
    
    # Pre-populate cache
    from job_bot.schemas.job import AIScore
    cached_score = AIScore(score=99, reason="cached", strengths=[], missing_skills=[], recommendation="Apply", improvement_tips="None")
    mock_scorer.cache[job.job_hash] = cached_score
    
    result = await mock_scorer.score_job(job, profile)
    
    assert result is cached_score
    mock_scorer._call_gemini.assert_not_called()
