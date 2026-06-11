#!/bin/bash

# Exit on any error
set -e

# Function for logging with timestamp
log_message() {
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] $1"
}

# Function for error logging
error_message() {
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] ERROR: $1" >&2
}

# Function to check if command succeeded
check_command() {
    if [ $? -ne 0 ]; then
        error_message "$1 failed"
        exit 1
    fi
}

# Get the project root directory
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "${SCRIPT_DIR}/../.." && pwd)"

# Set paths
OUTPUT_DIR="/data/sharing/AGOL/wpt_layers/"
QUALIFIERS=("AmzFACE" "BCI" "Ducke" "Gault" "SBL" "StJoseph" "TBS" "Yasuni" "ZF2")
AGOL_PROJECTS=("BCI" "Ducke" "TBS")
MAX_WORKERS=24

source /opt/miniconda3/bin/activate arbutus
check_command "conda activate arbutus"

for QUALIFIER in "${QUALIFIERS[@]}"; do
    log_message "Processing waypoint missions for $QUALIFIER"
    python "$PROJECT_ROOT/scripts/python/arbutus2points.py" \
        --output_dir "$OUTPUT_DIR" \
        --project_qualifier "$QUALIFIER" \
        --max_workers $MAX_WORKERS
    check_command "arbutus2points.py for $QUALIFIER"
done

conda deactivate
check_command "conda deactivate"

source /opt/miniconda3/bin/activate AGOL
check_command "conda activate AGOL"

for AGOL_PROJECT in "${AGOL_PROJECTS[@]}"; do
    log_message "Converting and zipping ${AGOL_PROJECT}_wpt.gpkg"
    python "$PROJECT_ROOT/scripts/python/gpkg2shp.py" "$OUTPUT_DIR/${AGOL_PROJECT}_wpt.gpkg" "$OUTPUT_DIR"
    check_command "gpkg2shp.py for $AGOL_PROJECT"

    log_message "Updating AGOL layer for $AGOL_PROJECT"
    python "$PROJECT_ROOT/scripts/python/update_AGOL.py" \
        --project_name "$AGOL_PROJECT" \
        --shp_path "$OUTPUT_DIR"
    check_command "update_AGOL.py for $AGOL_PROJECT"
done