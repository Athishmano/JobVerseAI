"""
┌─ FILE: main.py
├─ PURPOSE: Entry point for the JobBot CLI.
├─ USED BY: User (e.g., `python main.py --limit 50 --debug`)
├─ DATA FLOW: argparse -> setup_logging -> load_env -> run_pipeline
├─ DESIGN DECISIONS: CLI acts only as the trigger. No scheduler or background jobs.
└─ PATTERNS: Argparse, standard python entry point (`if __name__ == '__main__':`)
"""

import argparse
import asyncio
import logging
import sys

from job_bot.core.config import load_env
from job_bot.core.dashboard_launcher import launch_dashboard
from job_bot.core.logger import set_debug_mode, setup_logging
from job_bot.services.pipeline import run_pipeline

def main():
    parser = argparse.ArgumentParser(
        description="JobBot: AI-powered job scraper and scorer."
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=25,
        help=(
            "Number of top candidates to AI-score per run (default: 25). "
            "Scrapers collect ALL available jobs first; this controls how many "
            "of the best offline-ranked jobs get the expensive Gemini scoring call. "
            "Higher values = more thorough but slower (each job takes ~4-5s to score). "
            "Recommended: 50 for a thorough run, 25 for a quick check."
        )
    )
    parser.add_argument(
        "--sites",
        type=str,
        default="all",
        help="Comma-separated list of portals to scrape (naukri, linkedin, indeed, wellfound) or 'all' (default: all)"
    )
    parser.add_argument(
        "--debug",
        action="store_true",
        help="Enable debug logging and rich tracebacks"
    )

    args = parser.parse_args()

    # 1. Setup logging
    setup_logging(debug=args.debug)
    logger = logging.getLogger("main")

    # 2. Load settings (fails fast if GEMINI_API_KEY is missing)
    try:
        settings = load_env()
    except Exception as e:
        logger.error("Failed to load environment configuration: %s", e)
        sys.exit(1)

    if args.debug:
        set_debug_mode(True)
        logger.debug("Debug mode enabled.")

    # 3. Run Pipeline
    try:
        # run_pipeline returns the timestamp
        timestamp = asyncio.run(run_pipeline(settings=settings, limit_per_site=args.limit, sites=args.sites))
        if timestamp:
            launch_dashboard(timestamp)
    except KeyboardInterrupt:
        logger.warning("Pipeline interrupted by user (Ctrl+C). Exiting...")
        sys.exit(130)
    except Exception as e:
        logger.exception("Fatal error in pipeline execution: %s", e)
        sys.exit(1)

if __name__ == "__main__":
    main()
