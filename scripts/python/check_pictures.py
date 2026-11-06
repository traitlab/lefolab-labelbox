"""Check close-up/wide/med picture pairing of a mission on lefodata, before the transfer to Arbutus."""

import argparse
import os
import sys

from _common import DROP_UNMATCHED_EXIT_CODE, confirm_drop_unmatched, pair_pictures, setup_logging

logger = setup_logging()

LEFODATA_ROOT = "/mnt/nfs/lefodata/data"

parser = argparse.ArgumentParser(description="Check picture pairing of a mission on lefodata.")
parser.add_argument("--mission", required=True, help="Mission ID of the pictures to check.")
args = parser.parse_args()

mission = args.mission
mission_dir = f"{LEFODATA_ROOT}/drone_missions/{mission[:4]}/{mission}"

# Keys relative to LEFODATA_ROOT, same layout as the Arbutus keys (drone_missions/<yyyy>/<mission>/...)
file_keys = [
    os.path.relpath(os.path.join(root, name), LEFODATA_ROOT)
    for root, _, names in os.walk(mission_dir)
    for name in names
    if name.lower().endswith(".jpg")
]

pairs, mismatch = pair_pictures(file_keys, mission)

if mismatch:
    confirm_drop_unmatched(mission)
    sys.exit(DROP_UNMATCHED_EXIT_CODE)

logger.info(f"All {len(pairs)} close-up pictures are matched for this mission : {mission}")
