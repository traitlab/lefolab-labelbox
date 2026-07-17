import argparse
import logging
import sys

from pathlib import Path

# Make scripts/python importable for _common
sys.path.insert(0, str(Path(__file__).parent.parent / "scripts" / "python"))

import labelbox as lb

from _common import get_client

# Logging goes to stderr: stdout is reserved for the mission list,
# so shell scripts can capture it directly.
logging.basicConfig(
    stream=sys.stderr,
    level=logging.INFO,
    format="[%(asctime)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger()

parser = argparse.ArgumentParser(
    description="List the distinct mission ids referenced by the data rows of a "
                "Labelbox dataset (datasets are named after the project). "
                "Prints one mission per line to stdout."
)
parser.add_argument("--project", required=True, help="Project name (= Labelbox dataset name)")
args = parser.parse_args()

client = get_client()

datasets = [ds for ds in client.get_datasets() if ds.name == args.project]
if not datasets:
    logger.error(f"Labelbox dataset '{args.project}' not found.")
    sys.exit(1)
if len(datasets) > 1:
    logger.error(f"Multiple datasets named '{args.project}' found ({len(datasets)}). Cannot disambiguate.")
    sys.exit(1)
dataset = datasets[0]

missions = set()

def handle_export(output: lb.BufferedJsonConverterOutput):
    row = output.json
    for mf in row.get("metadata_fields", []):
        if mf.get("schema_name") == "mission" and mf.get("value"):
            missions.add(mf["value"])

export_task = dataset.export(params={"metadata_fields": True})
export_task.wait_till_done()

if export_task.has_errors():
    export_task.get_buffered_stream(stream_type=lb.StreamType.ERRORS).start(
        stream_handler=lambda error: logger.error(f"Export error: {error.json}")
    )
    sys.exit(1)

export_task.get_buffered_stream().start(stream_handler=handle_export)

if not missions:
    logger.error(f"No missions found in dataset '{args.project}'.")
    sys.exit(1)

logger.info(f"{len(missions)} missions found in dataset '{args.project}'.")

for mission in sorted(missions):
    print(mission)
