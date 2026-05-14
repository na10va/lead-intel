"""
utils/logger.py — Centralized logging for all agents and pipeline components.

Every agent, enrichment step, and maintenance script should use this module
instead of print() or the stdlib logging module directly. This ensures a
consistent format across all log output and log files.

Usage:
    from utils.logger import get_logger

    log = get_logger("probate_agent")
    log.info("Fetched 12 new records from Cuyahoga County Probate Court")
    log.warning("Parcel ID missing on record 7 — flagged for manual review")
    log.error("Source returned HTTP 403 — flagging as blocked")
"""

import logging
import os
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path


# ---------------------------------------------------------------------------
# Log directory — written to lead-intel/logs/ relative to this file's package root
# ---------------------------------------------------------------------------
_ROOT = Path(__file__).resolve().parent.parent
_LOG_DIR = _ROOT / "logs"
_LOG_DIR.mkdir(exist_ok=True)


def get_logger(name: str) -> logging.Logger:
    """Return a configured logger for the given component name.

    Args:
        name: Short identifier for the component, e.g. "probate_agent",
              "enrichment.waterfall", "scheduler". Used as the logger name
              and appears in every log line.

    Returns a Logger that writes to both stdout and a rotating file at
    logs/{name}.log (10 MB max, 3 backups kept).

    Log format:
        2026-04-16 07:00:01 [probate_agent] INFO  Fetched 12 records
    """
    logger = logging.getLogger(name)

    # Avoid adding duplicate handlers if get_logger is called multiple times
    if logger.handlers:
        return logger

    logger.setLevel(logging.DEBUG)

    formatter = logging.Formatter(
        fmt="%(asctime)s [%(name)s] %(levelname)-5s %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    # Stdout handler — INFO and above
    stdout_handler = logging.StreamHandler(sys.stdout)
    stdout_handler.setLevel(logging.INFO)
    stdout_handler.setFormatter(formatter)

    # File handler — DEBUG and above, rotating at 10 MB, keeping 3 backups
    log_file = _LOG_DIR / f"{name.replace('.', '_')}.log"
    file_handler = RotatingFileHandler(
        log_file,
        maxBytes=10 * 1024 * 1024,  # 10 MB
        backupCount=3,
        encoding="utf-8",
    )
    file_handler.setLevel(logging.DEBUG)
    file_handler.setFormatter(formatter)

    logger.addHandler(stdout_handler)
    logger.addHandler(file_handler)

    return logger
