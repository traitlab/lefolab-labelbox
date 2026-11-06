"""Shared helpers for the Labelbox scripts in scripts/python/.

These scripts are run directly (e.g. ``python scripts/python/move_datarows.py``),
so their own directory is on ``sys.path`` and ``from _common import ...`` works
without any packaging step.
"""

from __future__ import annotations

import logging
import os
import sys
import time

from contextlib import redirect_stderr
from io import BytesIO, StringIO
from pathlib import Path
from typing import TYPE_CHECKING

import exifread
import requests
from dotenv import load_dotenv
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

# labelbox is imported lazily so envs without it (e.g. arbutus) can use the rest
if TYPE_CHECKING:
    import labelbox as lb

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


# Exit codes checked by complete_workflow.sh
MISMATCH_EXIT_CODE = 10       # picture mismatch, skip the mission
DROP_UNMATCHED_EXIT_CODE = 11 # mismatch accepted, drop the unmatched pictures


def pair_pictures(file_keys: list[str], mission: str) -> tuple[list[tuple], bool]:
    """Pair each close-up picture with its wide (required) and med (optional) picture.

    Returns (pairs, mismatch): pairs is a list of (closeup, wide, med, polygon_id) for
    close-ups that have a wide picture; mismatch is True if some pictures are unmatched.
    Exits with MISMATCH_EXIT_CODE on duplicate wide/med pictures, 1 if no close-ups.
    """
    logger = logging.getLogger()
    logger.info(f"{len(file_keys)} pictures found for this mission : {mission}")

    # Filter for close-up pictures (detect naming convention)
    closeup_files = [key for key in file_keys if "tele" in key]
    if closeup_files:
        exclusion_keyword = "tele"
        logger.info(f"{len(closeup_files)} close-up pictures (tele) found for this mission : {mission}")
    else:
        closeup_files = [key for key in file_keys if "zoom" in key]
        if closeup_files:
            exclusion_keyword = "zoom"
            logger.info("Using legacy naming convention (zoom).")
            logger.info(f"{len(closeup_files)} close-up pictures (zoom) found for this mission : {mission}")
        else:
            logger.error(f"No close-up pictures found for this mission : {mission}")
            sys.exit(1)

    # Verify that wide (and med) picture counts match close-up count
    if exclusion_keyword == "tele":
        wide_files_all = [key for key in file_keys if "wide" in key.lower()]
    else:
        # Legacy zoom: wide files have no keyword, so exclude zoom and med
        wide_files_all = [key for key in file_keys if "zoom" not in key.lower() and "med" not in key.lower()]

    med_files_all = [key for key in file_keys if "med" in key.lower()]

    logger.info(f"{len(wide_files_all)} wide pictures found for this mission : {mission}")

    mismatch = False
    if len(wide_files_all) != len(closeup_files):
        logger.error(f"Picture count mismatch: {len(closeup_files)} close-up vs {len(wide_files_all)} wide pictures.")
        mismatch = True

    if med_files_all:
        logger.info(f"{len(med_files_all)} med pictures found for this mission : {mission}")
        if len(med_files_all) != len(closeup_files):
            logger.error(f"Picture count mismatch: {len(closeup_files)} close-up vs {len(med_files_all)} med pictures.")
            mismatch = True

    pairs = []
    for closeup_file in closeup_files:
        # Extract the polygon id and determine matching file suffixes
        if exclusion_keyword == "tele":
            polygon_id = closeup_file.split('_')[-1].lower().replace('tele.jpg', '')
            wide_file_end = f"_{polygon_id}wide.JPG"
        else:
            # Legacy naming convention (zoom)
            polygon_id = closeup_file.split('_')[-1].lower().replace('zoom.jpg', '')
            wide_file_end = f"_{polygon_id}.JPG"

        matching_wide_files = [key for key in file_keys if wide_file_end in key and exclusion_keyword not in key]
        if len(matching_wide_files) > 1:
            logger.error(f"Multiple wide pictures found for {closeup_file}: {matching_wide_files}. Mission {mission} not added to Labelbox.")
            sys.exit(MISMATCH_EXIT_CODE)
        if not matching_wide_files:
            logger.warning(f"No wide file found for {closeup_file}.")
            mismatch = True
            continue

        # Find the corresponding med file (optional)
        med_file_end = f"_{polygon_id}med.JPG"
        matching_med_files = [key for key in file_keys if med_file_end in key]
        if len(matching_med_files) > 1:
            logger.error(f"Multiple med pictures found for {closeup_file}: {matching_med_files}. Mission {mission} not added to Labelbox.")
            sys.exit(MISMATCH_EXIT_CODE)
        med_file = matching_med_files[0] if matching_med_files else None

        pairs.append((closeup_file, matching_wide_files[0], med_file, polygon_id))

    used_files = {f for _, wide, med, _ in pairs for f in (wide, med)}
    for key in wide_files_all + med_files_all:
        if key not in used_files:
            logger.warning(f"No close-up file found for {key}.")

    return pairs, mismatch


def confirm_drop_unmatched(mission: str) -> None:
    """Ask to drop unmatched pictures if interactive; otherwise exit with MISMATCH_EXIT_CODE."""
    if sys.stdin.isatty() and input("Continue and drop the unmatched pictures? [y/N] ").strip().lower() == "y":
        return
    logging.getLogger().error(f"Mission {mission} not added to Labelbox.")
    sys.exit(MISMATCH_EXIT_CODE)


def get_client() -> lb.Client:
    """Return an authenticated Labelbox client, or exit if the API key is unset."""
    import labelbox as lb

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


def convert_to_decimal_degrees(value, ref):
    """Convert an EXIF GPS coordinate (degrees, minutes, seconds) and its N/S/E/W ref to decimal degrees."""
    if len(value.values) != 3:
        raise ValueError("Malformed or incomplete EXIF data: GPS coordinate value does not contain exactly three elements")
    d, m, s = [float(x.num) / float(x.den) for x in value.values]
    decimal_degrees = d + (m / 60) + (s / 3600)
    if ref.values and ref.values[0] in ['S', 'W']:
        decimal_degrees = -decimal_degrees
    return decimal_degrees


def setup_session() -> requests.Session:
    """Create a requests session that retries on 429 and 5xx responses."""
    session = requests.Session()
    retry_strategy = Retry(
        total=3,
        status_forcelist=[429, 500, 502, 503, 504],
        allowed_methods=["HEAD", "GET", "OPTIONS"],
        backoff_factor=1
    )
    adapter = HTTPAdapter(max_retries=retry_strategy)
    session.mount("http://", adapter)
    session.mount("https://", adapter)
    return session


def safe_request(session, url, timeout=30, max_retries=3):
    """GET a URL, retrying on connection errors with exponential backoff; None if all attempts fail."""
    logger = logging.getLogger()
    for attempt in range(max_retries):
        try:
            return session.get(url, timeout=timeout)
        except requests.exceptions.RequestException as e:
            logger.error(f"Request failed (attempt {attempt + 1}/{max_retries}): {e}")
            if attempt < max_retries - 1:
                time.sleep(2 ** attempt)
            else:
                logger.error(f"Failed to fetch {url} after {max_retries} attempts")
                return None


def get_coordinates_from_image_url(picture_url, session=None):
    """Return (latitude, longitude) from an image's EXIF GPS tags, or None on any failure (logged)."""
    logger = logging.getLogger()
    response = safe_request(session or setup_session(), picture_url)

    if response is None:
        return None
    if response.status_code != 200:
        logger.error(f"Failed to fetch image. HTTP Status Code: {response.status_code} for {picture_url}")
        return None

    try:
        with redirect_stderr(StringIO()):
            tags = exifread.process_file(BytesIO(response.content), details=False)
    except (IndexError, KeyError, ValueError) as e:
        logger.error(f"Error processing EXIF data: {e} for {picture_url}")
        return None

    latitude = tags.get('GPS GPSLatitude')
    latitude_ref = tags.get('GPS GPSLatitudeRef')
    longitude = tags.get('GPS GPSLongitude')
    longitude_ref = tags.get('GPS GPSLongitudeRef')
    if not (latitude and latitude_ref and longitude and longitude_ref):
        logger.warning(f"Missing GPS EXIF tags in the image metadata for {picture_url.split('/')[-1]}.")
        return None

    try:
        return convert_to_decimal_degrees(latitude, latitude_ref), convert_to_decimal_degrees(longitude, longitude_ref)
    except (ValueError, AttributeError) as e:
        logger.error(f"Error converting GPS coordinates: {e} for {picture_url}")
        return None
