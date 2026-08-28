"""Compare the waypoint missions held on lefodata, the legacy Arbutus and the new Arbutus.

Nothing is written to any of the three sources: this only lists and counts, so it is
safe to run at any time. lefodata is treated as the reference; the report tells you
whether every legacy mission reached the new Arbutus intact before anything is deleted.

Layouts covered:
  lefodata        $LEFODATA_PATH/drone_missions/<year>/<mission>/**
  legacy (own)    AllianceCanBuckets:<mission>/**              (mission is its own bucket)
  legacy (wpt)    AllianceCanBuckets:$BUCKET_WPT/<mission>/**
  new             ArbutusBuckets:<project>/drone_missions/<year>/<mission>/**

Missions are selected on the substring 'wpt', plus the older 'waypoint' naming so the
pre-rename legacy buckets are not missed.

The derived `labelbox/` folder (overviews, generate_maps logs, zoom attachments) is
produced by this repo and never exists on lefodata, so it is counted in its own columns
and excluded from the raw-data comparison.

Inventories are cached under cache/ so re-runs are instant; use --refresh to rescan.

Usage:
    python compare_wpt_missions.py [--refresh {all,lefodata,legacy,new}] [--output FILE]
"""

import argparse
import csv
import json
import os
import re
import subprocess
import sys

from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent / "scripts" / "python"))

from _common import PROJECT_ROOT, setup_logging

MISSION_PATTERN = re.compile(r"wpt|waypoint", re.IGNORECASE)
DERIVED_DIR = "labelbox/"
SEPARATOR = "|"

LEGACY_CONF = Path("/etc/rclone.conf")
LEGACY_REMOTE = "AllianceCanBuckets"
NEW_CONF = PROJECT_ROOT / "rclone_newArbutus.conf"
NEW_REMOTE = "ArbutusBuckets"

CACHE_DIR = Path(__file__).parent / "cache"
DEFAULT_OUTPUT = Path(__file__).parent / "wpt_missions_comparison.csv"

logger = setup_logging()


def rclone_files(config, remote_path):
    """Yield (size, path relative to remote_path) for every file below remote_path."""
    result = subprocess.run(
        ["rclone", "--config", str(config), "lsf", "-R", "--files-only",
         "--format", "sp", "--separator", SEPARATOR, remote_path],
        capture_output=True, text=True, check=True,
    )
    for line in result.stdout.splitlines():
        size, _, path = line.partition(SEPARATOR)
        yield int(size), path


def blank():
    return {"files": 0, "bytes": 0, "derived_files": 0, "derived_bytes": 0}


def add(stats, size, relative_path):
    """Count one file into stats, keeping the derived labelbox/ data apart."""
    key = "derived" if relative_path.startswith(DERIVED_DIR) else "raw"
    if key == "derived":
        stats["derived_files"] += 1
        stats["derived_bytes"] += size
    else:
        stats["files"] += 1
        stats["bytes"] += size


def scan_lefodata():
    """Inventory every wpt mission under $LEFODATA_PATH/drone_missions/<year>/."""
    root = Path(os.environ["LEFODATA_PATH"]) / "drone_missions"
    if not root.is_dir():
        logger.error(f"lefodata directory not found: {root}")
        sys.exit(1)

    missions = {}
    for year_dir in sorted(root.iterdir()):
        if not year_dir.is_dir():
            continue
        for mission_dir in sorted(year_dir.iterdir()):
            if not mission_dir.is_dir() or not MISSION_PATTERN.search(mission_dir.name):
                continue
            stats = blank()
            stats["year"] = year_dir.name
            for path in mission_dir.rglob("*"):
                if path.is_file():
                    add(stats, path.stat().st_size, str(path.relative_to(mission_dir)))
            missions[mission_dir.name] = stats
        logger.info(f"lefodata {year_dir.name}: {len(missions)} wpt missions so far")
    return missions


def scan_legacy():
    """Inventory the legacy Arbutus, where a mission is either its own bucket or a
    folder of $BUCKET_WPT."""
    bucket_wpt = os.environ["BUCKET_WPT"]

    buckets = subprocess.run(
        ["rclone", "--config", str(LEGACY_CONF), "lsf", "--dirs-only", f"{LEGACY_REMOTE}:"],
        capture_output=True, text=True, check=True,
    ).stdout.split()
    own_buckets = [b.rstrip("/") for b in buckets
                   if MISSION_PATTERN.search(b) and b.rstrip("/") != bucket_wpt]
    logger.info(f"legacy: listing {len(own_buckets)} mission buckets")

    def scan_bucket(bucket):
        stats = blank()
        for size, path in rclone_files(LEGACY_CONF, f"{LEGACY_REMOTE}:{bucket}"):
            add(stats, size, path)
        return bucket, stats

    with ThreadPoolExecutor(max_workers=8) as pool:
        own = dict(pool.map(scan_bucket, own_buckets))

    logger.info(f"legacy: listing the {bucket_wpt} bucket")
    wpt = {}
    for size, path in rclone_files(LEGACY_CONF, f"{LEGACY_REMOTE}:{bucket_wpt}"):
        mission, _, relative_path = path.partition("/")
        if not relative_path or not MISSION_PATTERN.search(mission):
            continue
        add(wpt.setdefault(mission, blank()), size, relative_path)

    logger.info(f"legacy: {len(own)} own buckets, {len(wpt)} missions in {bucket_wpt}")
    return {"own": own, "wpt": wpt}


def scan_new():
    """Inventory the new Arbutus, where each project bucket holds
    drone_missions/<year>/<mission>/."""
    buckets = subprocess.run(
        ["rclone", "--config", str(NEW_CONF), "lsf", "--dirs-only", f"{NEW_REMOTE}:"],
        capture_output=True, text=True, check=True,
    ).stdout.split()

    missions = {}
    for bucket in [b.rstrip("/") for b in buckets]:
        logger.info(f"new: listing bucket {bucket}")
        for size, path in rclone_files(NEW_CONF, f"{NEW_REMOTE}:{bucket}"):
            parts = path.split("/", 3)
            if len(parts) < 4 or parts[0] != "drone_missions":
                logger.warning(f"new: unexpected path in {bucket}: {path}")
                continue
            _, year, mission, relative_path = parts
            if not MISSION_PATTERN.search(mission):
                continue
            stats = missions.setdefault(mission, blank())
            stats["bucket"], stats["year"] = bucket, year
            add(stats, size, relative_path)

    logger.info(f"new: {len(missions)} wpt missions across {len(buckets)} buckets")
    return missions


def inventory(name, scan, refresh):
    """Return the inventory of one source, rebuilding its cache when asked."""
    cache = CACHE_DIR / f"{name}.json"
    if cache.exists() and refresh not in ("all", name):
        logger.info(f"{name}: reusing cache {cache}")
        return json.loads(cache.read_text())

    logger.info(f"{name}: scanning")
    data = scan()
    CACHE_DIR.mkdir(exist_ok=True)
    cache.write_text(json.dumps(data, indent=2, sort_keys=True))
    logger.info(f"{name}: cached to {cache}")
    return data


def status_flags(lefodata, own, wpt, new):
    """Describe how one mission differs across the sources."""
    flags = []
    on_legacy = bool(own or wpt)

    if own and wpt:
        flags.append("LEGACY_IN_BOTH")
    # An empty legacy bucket is not a partial transfer: the mission was never uploaded
    if own and not own["files"] and not own["derived_files"]:
        flags.append("LEGACY_BUCKET_EMPTY")
    if not lefodata:
        flags.append("NOT_ON_LEFODATA")
    if not on_legacy and not new:
        flags.append("LEFODATA_ONLY")
    if on_legacy and not new:
        flags.append("MISSING_ON_NEW")

    if lefodata:
        reference = (lefodata["files"], lefodata["bytes"])
        for label, stats in (("LEGACY_OWN", own), ("LEGACY_WPT", wpt), ("NEW", new)):
            if stats and (stats["files"], stats["bytes"]) != reference:
                flags.append(f"{label}_DIFFERS_FROM_LEFODATA")

    if new and on_legacy:
        source = own or wpt
        if (new["files"], new["bytes"]) != (source["files"], source["bytes"]):
            flags.append("NEW_DIFFERS_FROM_LEGACY")

    return flags or ["OK"]


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--refresh", choices=["all", "lefodata", "legacy", "new"],
                        help="Rescan this source instead of reusing its cache")
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT, help="CSV report to write")
    args = parser.parse_args()

    for name in ("BUCKET_WPT", "LEFODATA_PATH"):
        if not os.getenv(name):
            logger.error(f"{name} is not set in .env")
            sys.exit(1)
    for conf in (LEGACY_CONF, NEW_CONF):
        if not conf.is_file():
            logger.error(f"rclone config not found: {conf}")
            sys.exit(1)

    lefodata = inventory("lefodata", scan_lefodata, args.refresh)
    legacy = inventory("legacy", scan_legacy, args.refresh)
    new = inventory("new", scan_new, args.refresh)

    missions = sorted(set(lefodata) | set(legacy["own"]) | set(legacy["wpt"]) | set(new))
    counts = Counter()
    totals = Counter()

    with args.output.open("w", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow([
            "mission", "year", "naming",
            "lefodata_files", "lefodata_bytes",
            "legacy_own_files", "legacy_own_bytes",
            "legacy_wpt_files", "legacy_wpt_bytes",
            "new_bucket", "new_files", "new_bytes",
            "legacy_labelbox_files", "new_labelbox_files",
            "status",
        ])
        for mission in missions:
            reference = lefodata.get(mission)
            own = legacy["own"].get(mission)
            wpt = legacy["wpt"].get(mission)
            target = new.get(mission)
            flags = status_flags(reference, own, wpt, target)
            counts.update(flags)
            for label, stats in (("lefodata", reference), ("legacy", own or wpt), ("new", target)):
                if stats:
                    totals[f"{label}_missions"] += 1
                    totals[f"{label}_files"] += stats["files"]
                    totals[f"{label}_bytes"] += stats["bytes"]

            year = (reference or target or {}).get("year") or mission[:4]
            naming = "wpt" if "wpt" in mission.lower() else "waypoint (old naming)"
            legacy_derived = (own or blank())["derived_files"] + (wpt or blank())["derived_files"]

            writer.writerow([
                mission, year, naming,
                *([reference["files"], reference["bytes"]] if reference else ["", ""]),
                *([own["files"], own["bytes"]] if own else ["", ""]),
                *([wpt["files"], wpt["bytes"]] if wpt else ["", ""]),
                *([target["bucket"], target["files"], target["bytes"]] if target else ["", "", ""]),
                legacy_derived,
                target["derived_files"] if target else "",
                ";".join(flags),
            ])

    logger.info(f"{len(missions)} wpt missions compared, report written to {args.output}")

    logger.info("Raw data present per source (labelbox/ excluded):")
    for label in ("lefodata", "legacy", "new"):
        logger.info(f"  {label:9} {totals[f'{label}_missions']:5d} missions  "
                    f"{totals[f'{label}_files']:8d} files  "
                    f"{totals[f'{label}_bytes'] / 1e12:7.2f} TB")

    logger.info("Status:")
    for flag, count in counts.most_common():
        logger.info(f"  {count:5d}  {flag}")


if __name__ == "__main__":
    main()
