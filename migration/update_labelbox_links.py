"""Rewrite Labelbox row_data and attachment URLs from the legacy Arbutus to the
new Arbutus (<project with - instead of _>/drone_missions/<yyyy>/<mission>/...).

Handles both dataset conventions: one dataset named after the project, or one
dataset per mission named <project>_<mission>. Legacy source URLs may point to
$BUCKET_WPT/<mission>/... or to the mission's own bucket <mission>/...

Dry run by default (prints planned changes); pass --apply to update Labelbox.
"""

import argparse
import logging
import os
import re
import sys

from pathlib import Path

# Make scripts/python importable for _common
sys.path.insert(0, str(Path(__file__).parent.parent / "scripts" / "python"))

import labelbox as lb

from _common import get_client, setup_logging

OLD_BASE = "https://object-arbutus.cloud.computecanada.ca"

logger = setup_logging()

NEW_BASE = os.getenv("ALLIANCECAN_URL")
if not NEW_BASE:
    logger.error("ALLIANCECAN_URL environment variable is not set")
    sys.exit(1)

BUCKET_WPT = os.getenv("BUCKET_WPT")
if not BUCKET_WPT:
    logger.error("BUCKET_WPT environment variable is not set")
    sys.exit(1)

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument("--project", required=True, help="Project name (dataset name or dataset name prefix)")
parser.add_argument("--apply", action="store_true", help="Apply the changes (default: dry run)")
args = parser.parse_args()

new_bucket = args.project.replace("_", "-")


def convert_url(url):
    """Return the new Arbutus URL, or None if the URL is not a legacy Arbutus URL."""
    if not url or not url.startswith(f"{OLD_BASE}/"):
        return None
    path = url[len(f"{OLD_BASE}/"):]
    first, _, rest = path.partition("/")
    if first == BUCKET_WPT:
        mission, _, rest = rest.partition("/")
    else:
        mission = first
    if not re.match(r"^\d{8}_", mission) or not rest:
        return None
    return f"{NEW_BASE}/{new_bucket}/drone_missions/{mission[:4]}/{mission}/{rest}"


client = get_client()

datasets = [ds for ds in client.get_datasets()
            if ds.name == args.project or ds.name.startswith(f"{args.project}_")]
if not datasets:
    logger.error(f"No dataset named '{args.project}' or '{args.project}_<mission>' found.")
    sys.exit(1)

logger.info(f"{len(datasets)} dataset(s) found: {', '.join(sorted(ds.name for ds in datasets))}")

total_changed = 0
unconvertible = []

for dataset in datasets:
    logger.info(f"Exporting dataset {dataset.name}")
    rows = []
    export_task = dataset.export(params={"attachments": True})
    export_task.wait_till_done()
    if export_task.has_errors():
        export_task.get_buffered_stream(stream_type=lb.StreamType.ERRORS).start(
            stream_handler=lambda error: logger.error(f"Export error: {error.json}")
        )
        sys.exit(1)
    export_task.get_buffered_stream().start(stream_handler=lambda o: rows.append(o.json))

    items = []
    examples = []
    for row in rows:
        item = {"key": lb.UniqueId(row["data_row"]["id"])}

        old_row_data = row["data_row"]["row_data"]
        new_row_data = convert_url(old_row_data)
        if new_row_data:
            item["row_data"] = new_row_data
            examples.append((old_row_data, new_row_data))
        elif old_row_data.startswith(OLD_BASE):
            unconvertible.append(old_row_data)

        attachments = row.get("attachments") or []
        new_attachments = []
        attachments_changed = False
        for att in attachments:
            new_value = convert_url(att["value"])
            if new_value:
                attachments_changed = True
            elif att["value"].startswith(OLD_BASE):
                unconvertible.append(att["value"])
            new_attachments.append({"type": att["type"],
                                    "value": new_value or att["value"],
                                    "name": att["name"]})
        if attachments_changed:
            item["attachments"] = new_attachments

        if len(item) > 1:
            items.append(item)

    logger.info(f"{dataset.name}: {len(items)}/{len(rows)} data rows to update")
    if examples:
        logger.info(f"Example: {examples[0][0]}")
        logger.info(f"      -> {examples[0][1]}")

    if not args.apply or not items:
        continue

    logger.info(f"Updating {len(items)} data rows in {dataset.name}")
    task = dataset.upsert_data_rows(items)
    task.wait_till_done()
    if task.errors:
        logger.error(f"Upsert errors for {dataset.name}: {task.errors}")
        sys.exit(1)
    total_changed += len(items)
    logger.info(f"{dataset.name} updated")

if unconvertible:
    logger.warning(f"{len(unconvertible)} legacy URLs could not be converted and were left unchanged, e.g.:")
    for url in unconvertible[:5]:
        logger.warning(f"  {url}")

if args.apply:
    logger.info(f"Done: {total_changed} data rows updated. Re-run without --apply to verify no legacy URL remains.")
else:
    logger.info("Dry run: nothing was changed on Labelbox. Re-run with --apply to update.")
