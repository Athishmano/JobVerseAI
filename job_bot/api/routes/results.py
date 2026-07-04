"""
┌─ FILE: job_bot/api/routes/results.py
├─ PURPOSE: FastAPI router exposing JSON results of past job scraping runs.
├─ USED BY: job_bot/api/main.py -> Frontend Dashboard
├─ DATA FLOW: GET request -> storage.py -> JSON response
├─ DESIGN DECISIONS: Read-only minimal API. All logic delegates to storage.py.
└─ PATTERNS: FastAPI APIRouter, straightforward delegation, explicit 404s.
"""

from fastapi import APIRouter, HTTPException

from job_bot.core import storage

router = APIRouter(prefix="/api/v1/results", tags=["Results"])


@router.get("")
async def get_all_runs() -> list[str]:
    """
    Returns a list of all successful run timestamps, newest first.
    Example: ["30-06-2026_14-30-00", "29-06-2026_09-15-22"]
    """
    try:
        return storage.list_runs()
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/{timestamp}")
async def get_run_details(timestamp: str) -> dict:
    """
    Fetch both the Passed and Failed jobs for a specific run timestamp.
    
    Response shape matches storage.get_run():
    {
        "timestamp": "...",
        "passed": { ... },
        "failed": { ... }
    }
    """
    try:
        return storage.get_run(timestamp)
    except FileNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
