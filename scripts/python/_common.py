"""Shared helpers for the Labelbox scripts in scripts/python/.

These scripts are run directly (e.g. ``python scripts/python/move_datarows.py``),
so their own directory is on ``sys.path`` and ``from _common import ...`` works
without any packaging step.
"""

import logging
import os
import sys

from pathlib import Path

import labelbox as lb
from dotenv import load_dotenv

# Repo root (scripts/python/_common.py -> scripts/python -> scripts -> repo root).
PROJECT_ROOT = Path(__file__).parent.parent.parent

# Load environment variables from the repo-root .env once, on import.
load_dotenv(dotenv_path=PROJECT_ROOT / ".env")


def setup_logging() -> logging.Logger:
    """Configure the root logger: INFO to stdout, WARNING+ to stderr, timestamped.

    Returns the root logger. Safe to call once per script at startup; it resets
    handlers so repeated calls do not duplicate output.
    """
    logger = logging.getLogger()
    logger.setLevel(logging.INFO)

    stdout_handler = logging.StreamHandler(sys.stdout)
    stdout_handler.setLevel(logging.INFO)
    stdout_handler.addFilter(lambda record: record.levelno == logging.INFO)
    stdout_handler.setFormatter(logging.Formatter("[%(asctime)s] %(message)s", datefmt="%Y-%m-%d %H:%M:%S"))

    stderr_handler = logging.StreamHandler(sys.stderr)
    stderr_handler.setLevel(logging.WARNING)
    stderr_handler.setFormatter(logging.Formatter("[%(asctime)s] %(message)s", datefmt="%Y-%m-%d %H:%M:%S"))

    logger.handlers = []
    logger.addHandler(stdout_handler)
    logger.addHandler(stderr_handler)
    return logger


def get_client() -> lb.Client:
    """Return an authenticated Labelbox client, or exit if the API key is unset."""
    api_key = os.getenv("LABELBOX_API_KEY")
    if not api_key:
        logging.getLogger().error("LABELBOX_API_KEY environment variable is not set")
        sys.exit(1)
    return lb.Client(api_key=api_key)


def resolve_project(client: lb.Client, name: str):
    """Resolve a Labelbox project by exact name, or exit on zero / multiple matches."""
    projects = [p for p in client.get_projects() if p.name == name]
    if not projects:
        logging.getLogger().error(f"Labelbox project '{name}' not found.")
        sys.exit(1)
    if len(projects) > 1:
        logging.getLogger().error(f"Multiple projects named '{name}' found ({len(projects)}). Cannot disambiguate.")
        sys.exit(1)
    return projects[0]
