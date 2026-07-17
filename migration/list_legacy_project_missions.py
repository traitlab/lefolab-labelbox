import argparse
import logging
import re
import sys

from pathlib import Path

# Make scripts/python importable for _common
sys.path.insert(0, str(Path(__file__).parent.parent / "scripts" / "python"))

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
    description="List the missions of a legacy-workflow project, where each mission "
                "has its own Labelbox dataset named <project>_<mission>. "
                "Prints one mission per line to stdout."
)
parser.add_argument("--project", required=True, help="Project prefix of the dataset names")
args = parser.parse_args()

client = get_client()

prefix = f"{args.project}_"
missions = set()

for dataset in client.get_datasets():
    if not dataset.name.startswith(prefix):
        continue
    mission = dataset.name[len(prefix):]
    if re.match(r"^\d{8}_", mission):
        missions.add(mission)
    else:
        logger.warning(f"Skipping dataset '{dataset.name}': '{mission}' does not look like a mission id")

if not missions:
    logger.error(f"No datasets named '{prefix}<mission>' found.")
    sys.exit(1)

logger.info(f"{len(missions)} missions found for project '{args.project}'.")

for mission in sorted(missions):
    print(mission)
