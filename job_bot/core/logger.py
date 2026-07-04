"""
┌─ FILE: job_bot/core/logger.py
├─ PURPOSE: Configure Rich-based logging for the entire project and expose
│           helpers that enforce the mandatory [file.py:function] error-prefix
│           convention required by the spec.
├─ USED BY: Every module — each calls logging.getLogger(__name__).
│           main.py calls setup_logging() once at startup.
├─ DATA FLOW: setup_logging() → root logger + RichHandler → all module loggers inherit it
├─ DESIGN DECISIONS: markup=False on RichHandler prevents [file.py:fn] strings
│                    being misread as Rich markup tags. highlighter=None prevents
│                    unexpected regex-based colouring of structured output.
│                    log_error() / log_warning() are mandatory helpers so no module
│                    can forget the [file:fn] prefix — consistency is enforced by API.
└─ PATTERNS: Module-level loggers (getLogger(__name__)), single root handler,
             helper functions for uniform error formatting
"""

import io
import logging
import sys
from typing import Any

from rich.console import Console
from rich.logging import RichHandler

# ── Shared console ─────────────────────────────────────────────────────────────
# Wrap stdout in a UTF-8 TextIOWrapper so emoji in log messages are never blocked
# by Windows cp1252 console encoding.  force_terminal=True keeps colour support.
_utf8_stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", line_buffering=True)
_console = Console(file=_utf8_stdout, force_terminal=True)

# ── Sensitive-key detection ────────────────────────────────────────────────────
_SENSITIVE_SUBSTRINGS: frozenset[str] = frozenset(
    {
        "password",
        "api_key",
        "apikey",
        "token",
        "secret",
        "cookie",
        "credential",
        "auth",
        "passwd",
    }
)


def scrub_sensitive(data: dict[str, Any]) -> dict[str, Any]:
    """
    Return a shallow copy of *data* with values for sensitive keys replaced by
    '***REDACTED***'.  Works recursively for nested dicts.

    Call this before logging any dict that might contain credentials, API keys,
    or cookies — never log them raw.

    If *data* is not a dict, it is returned unchanged.
    """
    if not isinstance(data, dict):
        return data  # type: ignore[return-value]

    result: dict[str, Any] = {}
    for key, value in data.items():
        if any(sub in key.lower() for sub in _SENSITIVE_SUBSTRINGS):
            result[key] = "***REDACTED***"
        elif isinstance(value, dict):
            result[key] = scrub_sensitive(value)
        else:
            result[key] = value
    return result


# ── One-time setup ─────────────────────────────────────────────────────────────

def setup_logging(debug: bool = False) -> None:
    """
    Configure the root logger with a RichHandler that produces the exact format:

        HH:MM:SS | LEVEL    | message

    where LEVEL is left-justified to 8 characters:
        INFO     WARNING  ERROR    DEBUG

    Call this ONCE from main.py before any other module uses logging.
    After this call every ``logging.getLogger(__name__)`` in the project
    automatically inherits the handler through the root logger.

    Args:
        debug: If True, sets log level to DEBUG project-wide and enables
               local-variable display in Rich tracebacks.
    """
    level = logging.DEBUG if debug else logging.INFO

    handler = RichHandler(
        console=_console,
        show_time=False,            # time is embedded in our formatter string
        show_level=False,           # level is embedded in our formatter string
        show_path=False,            # no file-path column from Rich
        markup=False,               # [file.py:fn] must NOT be read as markup
        highlighter=None,           # disable regex-based syntax highlighting
        rich_tracebacks=True,       # pretty tracebacks on logger.exception()
        tracebacks_show_locals=debug,
    )
    formatter = logging.Formatter(
        fmt="%(asctime)s | %(levelname)-8s | %(message)s",
        datefmt="%H:%M:%S",
    )
    handler.setFormatter(formatter)

    root = logging.getLogger()
    root.setLevel(level)
    # Remove any pre-existing handlers (avoids duplicate output if setup is
    # called more than once, or if basicConfig was triggered by an import).
    root.handlers.clear()
    root.addHandler(handler)

    # Silence noisy third-party loggers that would otherwise flood the output
    for noisy in ("httpx", "httpcore", "playwright", "asyncio", "urllib3", "hpack"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


def set_debug_mode(enabled: bool) -> None:
    """
    Raise or lower the log level project-wide after initial setup.

    Called by main.py once ``--debug`` is parsed from argv.
    Also toggles local-variable display in Rich tracebacks.
    """
    level = logging.DEBUG if enabled else logging.INFO
    logging.getLogger().setLevel(level)
    for h in logging.getLogger().handlers:
        if isinstance(h, RichHandler):
            h.tracebacks_show_locals = enabled


# ── Mandatory error-formatting helpers ────────────────────────────────────────

def log_error(
    logger: logging.Logger,
    filename: str,
    function_name: str,
    message: str,
    action: str,
) -> None:
    """
    Log an ERROR with the mandatory traceable prefix required by the spec:

        HH:MM:SS | ERROR    | [filename:function_name] message — action

    Every try/except block that logs MUST use this helper.
    Never hand-roll the ``[file.py:function]`` string in individual modules —
    using this helper guarantees consistent formatting across the entire codebase.

    Example:
        log_error(logger, "naukri.py", "search",
                  "Timeout waiting for results on page 4",
                  "skipping page, continuing")

    Produces:
        14:15:40 | ERROR    | [naukri.py:search] Timeout waiting for results on page 4 — skipping page, continuing

    Args:
        logger:        Module-level logger (``logging.getLogger(__name__)``)
        filename:      Source file basename, e.g. ``"naukri.py"``
        function_name: Function where the error occurred, e.g. ``"search"``
        message:       What went wrong
        action:        Recovery action or ``"skipping"``
    """
    logger.error("[%s:%s] %s — %s", filename, function_name, message, action)


def log_warning(
    logger: logging.Logger,
    filename: str,
    function_name: str,
    message: str,
    action: str,
) -> None:
    """
    Same contract as log_error but at WARNING level.

    Use for recoverable issues where the run continues normally,
    e.g. an optional field missing from a job posting.

    Example:
        log_warning(logger, "naukri.py", "extract_jobs",
                    "applicant_count not found for job 98231",
                    "defaulting to null")

    Produces:
        14:15:01 | WARNING  | [naukri.py:extract_jobs] applicant_count not found for job 98231 — defaulting to null
    """
    logger.warning("[%s:%s] %s — %s", filename, function_name, message, action)
