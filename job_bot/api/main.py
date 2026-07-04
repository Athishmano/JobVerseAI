"""
┌─ FILE: job_bot/api/main.py
├─ PURPOSE: Main FastAPI application instance. Wires up routers and serves
│           the frontend static files for the dashboard.
├─ USED BY: uvicorn (via dashboard_launcher.py `python -m uvicorn job_bot.api.main:app`)
├─ DATA FLOW: HTTP Request -> FastAPI -> Router/StaticFiles -> HTTP Response
├─ DESIGN DECISIONS: Enables CORS so it can be developed against a separate dev server.
│                    Mounts the frontend/ dir at root so the UI is served automatically.
└─ PATTERNS: FastAPI init, CORS middleware, StaticFiles fallback.
"""

from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from job_bot.api.routes import results

# Setup the app
app = FastAPI(
    title="JobBot API",
    description="Minimal backend serving local scraping results to the dashboard.",
    version="1.0.0",
)

# Allow CORS for local dev servers
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# 1. Mount API routers
app.include_router(results.router)

# 2. Mount static frontend (Phase 13 output)
_ROOT = Path(__file__).resolve().parents[2]
_FRONTEND_DIR = _ROOT / "frontend"

# We check if frontend dir exists so the API doesn't crash during early development 
# before Phase 13 creates the frontend directory.
if _FRONTEND_DIR.exists():
    app.mount("/static", StaticFiles(directory=str(_FRONTEND_DIR), html=False), name="static")

    @app.get("/{full_path:path}")
    async def serve_frontend(full_path: str):
        """
        Catch-all route that serves index.html for any path not matching /api/
        This enables client-side routing (like /jobs?run=...) to work.
        """
        index_path = _FRONTEND_DIR / "index.html"
        
        # If it's a specific file request (like favicon.ico), try serving it directly
        requested_file = _FRONTEND_DIR / full_path
        if requested_file.is_file():
            return FileResponse(requested_file)
            
        return FileResponse(index_path)

@app.get("/")
async def root():
    """
    Fallback root if frontend is not yet built.
    """
    index_path = _FRONTEND_DIR / "index.html"
    if index_path.exists():
        return FileResponse(index_path)
    return {"message": "JobBot API is running. Frontend not yet built."}
