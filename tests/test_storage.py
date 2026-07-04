import os
import json
import pytest
from pathlib import Path
from job_bot.core import storage
from job_bot.schemas.job import Job, AIScore, RejectedJob
from job_bot.core.storage import PassedRunData, FailedRunData

@pytest.fixture
def temp_workspace(tmp_path, monkeypatch):
    """Override the base directories in storage.py to point to a tmp_path"""
    monkeypatch.setattr(storage, "PROFILE_PATH", tmp_path / "config" / "profile.json")
    monkeypatch.setattr(storage, "SEEN_JOBS_PATH", tmp_path / "data" / "seen_jobs.json")
    monkeypatch.setattr(storage, "SCORE_CACHE_PATH", tmp_path / "data" / "score_cache.json")
    monkeypatch.setattr(storage, "RESULTS_PASSED_DIR", tmp_path / "results" / "Passed")
    monkeypatch.setattr(storage, "RESULTS_FAILED_DIR", tmp_path / "results" / "Failed")
    
    return tmp_path

def test_cache_io(temp_workspace):
    assert storage.load_seen_jobs() == set()
    
    jobs = {"hash1", "hash2"}
    storage.save_seen_jobs(jobs, new_count=2)
    
    assert storage.load_seen_jobs() == {"hash1", "hash2"}

def test_save_run_data(temp_workspace):
    ts = "01-01-2026_12-00-00"
    
    job = Job(source="test", job_id="1", title="Dev", company="A", location="B", description="Good", apply_url="http://a")
    ai_score = AIScore(score=80, reason="good", strengths=["a"], missing_skills=["b"], recommendation="Apply", improvement_tips="None")
    
    passed_data = PassedRunData(sites_scraped=["test"], limit_per_site=10, total_scraped=1, total_failed=0, jobs=[(job, ai_score)])
    
    storage.save_passed(ts, passed_data)
    
    # Verify file was written
    passed_file = temp_workspace / "results" / "Passed" / f"{ts}.json"
    assert passed_file.exists()
    
    with open(passed_file, "r", encoding="utf-8") as f:
        data = json.load(f)
        assert data["total_scraped"] == 1
        assert len(data["best_matches"]) == 1
        assert data["best_matches"][0]["ai_score"] == 80
