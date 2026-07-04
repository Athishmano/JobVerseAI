"""
┌─ FILE: job_bot/core/dashboard_launcher.py
├─ PURPOSE: Starts the FastAPI dashboard backend, waits for it to be ready,
│           and opens the user's browser to the results of the current run.
├─ USED BY: main.py (at the very end of a successful run)
├─ DATA FLOW: main.py -> launch_dashboard(timestamp) -> subprocess -> webbrowser
├─ DESIGN DECISIONS: Uses a background subprocess so the main CLI process can exit
│                    or be closed without killing the dashboard immediately,
│                    although typically uvicorn will run until the terminal is closed.
│                    It checks if port 3000 is alive first to avoid port conflicts.
└─ PATTERNS: Polling with timeout, rich status spinner, webbrowser integration.
"""

import logging
import socket
import subprocess
import sys
import time
import urllib.request
import webbrowser
from pathlib import Path

from rich.console import Console

from job_bot.core.logger import log_error

logger = logging.getLogger(__name__)
_console = Console()

# The dashboard runs on port 3000 by default in our spec
PORT = 3000
BASE_URL = f"http://localhost:{PORT}"
API_ENTRY_POINT = "job_bot.api.main:app"

def is_port_in_use(port: int) -> bool:
    """Check if something is already bound to the given local port."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        return s.connect_ex(('127.0.0.1', port)) == 0

def is_dashboard_responding() -> bool:
    """Check if the dashboard server is returning 200 OK at the health endpoint or root."""
    try:
        # FastAPI will return something (even 404) if it's up.
        # We just need to know the HTTP layer is responding.
        req = urllib.request.Request(BASE_URL, method="GET")
        with urllib.request.urlopen(req, timeout=1.0) as response:
            return response.status in (200, 404)
    except Exception:
        return False

def launch_dashboard(timestamp: str) -> None:
    """
    Ensure the dashboard is running, wait for it to be ready, and open the browser.
    
    If the dashboard isn't responding on localhost:3000, it spawns uvicorn as a subprocess.
    """
    logger.info("Checking dashboard status on %s...", BASE_URL)
    
    frontend_process = None
    
    if not is_dashboard_responding():
        if is_port_in_use(PORT):
            log_error(
                logger, "dashboard_launcher.py", "launch",
                f"Port {PORT} is in use but not responding to HTTP. Cannot start dashboard.",
                "skipping dashboard launch"
            )
            return
            
        logger.info("Dashboard not running. Spawning background server...")
        
        # Spawn the FastAPI server via uvicorn
        # We use sys.executable to ensure it runs in the same python environment
        cmd = [sys.executable, "-m", "uvicorn", API_ENTRY_POINT, "--port", str(PORT)]
        
        try:
            # We redirect stdout/stderr so it doesn't spam the CLI where the user is looking
            # For debugging, one could write to a log file instead of DEVNULL.
            log_file = Path("dashboard.log")
            with open(log_file, "a") as f:
                frontend_process = subprocess.Popen(
                    cmd,
                    stdout=f,
                    stderr=subprocess.STDOUT
                )
        except Exception as e:
            log_error(
                logger, "dashboard_launcher.py", "launch",
                f"Failed to spawn uvicorn subprocess: {e}",
                "skipping dashboard launch"
            )
            return

        # Poll until ready using a rich spinner
        timeout = 10.0
        elapsed = 0.0
        poll_interval = 0.5
        
        with _console.status("[bold green]Starting dashboard...[/bold green]") as status:
            while elapsed < timeout:
                if is_dashboard_responding():
                    break
                    
                if frontend_process and frontend_process.poll() is not None:
                    # Process died
                    log_error(
                        logger, "dashboard_launcher.py", "launch",
                        "Dashboard process terminated unexpectedly. Check dashboard.log for details.",
                        "skipping dashboard launch"
                    )
                    return
                    
                time.sleep(poll_interval)
                elapsed += poll_interval

            if not is_dashboard_responding():
                log_error(
                    logger, "dashboard_launcher.py", "launch",
                    f"Dashboard failed to respond within {timeout} seconds.",
                    "skipping dashboard launch"
                )
                if frontend_process:
                    frontend_process.terminate()
                return

    # Dashboard is alive, open browser
    target_url = f"{BASE_URL}/jobs?run={timestamp}"
    logger.info("🌐 Opening dashboard: %s", target_url)
    
    try:
        webbrowser.open(target_url)
    except Exception as e:
        log_error(
            logger, "dashboard_launcher.py", "launch",
            f"Failed to open web browser: {e}",
            f"please navigate to {target_url} manually"
        )

    if frontend_process:
        _console.print("\n[bold cyan]🌐 Dashboard is running on port 3000.[/bold cyan]")
        _console.print("[bold cyan]Press Ctrl+C to stop the dashboard and exit.[/bold cyan]")
        try:
            frontend_process.wait()
        except KeyboardInterrupt:
            _console.print("\n[bold yellow]Stopping dashboard...[/bold yellow]")
            frontend_process.terminate()
            frontend_process.wait()
