import json
from job_bot.schemas.profile import UserProfile
from job_bot.schemas.job import Job
from job_bot.core.filter import apply_pre_filters

def test_profile_strips_comments():
    raw = {
        "_comment_1": "This should vanish",
        "personal": {
            "name": "Test User",
            "email": "test@test.com",
            "phone": "123",
            "location": "NY"
        },
        "summary": "Dev",
        "skills": ["Python"],
        "job_preferences": {
            "_comment_pref": "also vanish",
            "titles": ["Developer"],
            "locations": ["Remote"]
        }
    }
    profile = UserProfile.model_validate(raw)
    dumped = profile.model_dump()
    assert "_comment_1" not in dumped
    assert "_comment_pref" not in dumped["job_preferences"]

def test_apply_pre_filters_pass():
    profile = UserProfile.model_validate({
        "personal": {"name": "Test User", "email": "test@test.com", "phone": "123", "location": "NY"},
        "summary": "Dev",
        "skills": ["Python"],
        "job_preferences": {
            "titles": ["Developer"],
            "locations": ["Remote"],
            "blacklist_keywords": ["unpaid", "internship"],
            "blacklist_companies": ["tcs", "infosys"]
        }
    })
    
    job = Job(
        source="test",
        job_id="1",
        title="Python Developer",
        company="Acme Corp",
        location="Remote",
        description="Great job paying money",
        apply_url="http://example.com"
    )
    
    passed, reason, detail = apply_pre_filters(job, profile)
    assert passed is True
    assert reason == ""

def test_apply_pre_filters_blacklist_keyword():
    profile = UserProfile.model_validate({
        "personal": {"name": "Test User", "email": "test@test.com", "phone": "123", "location": "NY"},
        "summary": "Dev",
        "skills": ["Python"],
        "job_preferences": {
            "titles": ["Developer"],
            "locations": ["Remote"],
            "blacklist_keywords": ["unpaid", "internship"]
        }
    })
    
    job = Job(
        source="test",
        job_id="1",
        title="Unpaid Python Developer",
        company="Acme Corp",
        location="Remote",
        description="Great job",
        apply_url="http://example.com"
    )
    
    passed, reason, detail = apply_pre_filters(job, profile)
    assert passed is False
    assert reason == "blacklist_keyword"

def test_apply_pre_filters_blacklist_company():
    profile = UserProfile.model_validate({
        "personal": {"name": "Test User", "email": "test@test.com", "phone": "123", "location": "NY"},
        "summary": "Dev",
        "skills": ["Python"],
        "job_preferences": {
            "titles": ["Developer"],
            "locations": ["Remote"],
            "blacklist_companies": ["acme"]
        }
    })
    
    job = Job(
        source="test",
        job_id="1",
        title="Python Developer",
        company="Acme Corp",
        location="Remote",
        description="Great job",
        apply_url="http://example.com"
    )
    
    passed, reason, detail = apply_pre_filters(job, profile)
    assert passed is False
    assert reason == "blacklist_company"
