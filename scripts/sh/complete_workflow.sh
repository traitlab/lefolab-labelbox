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

# Load environment variables from .env file
if [ -f "${PROJECT_ROOT}/.env" ]; then
    set -a
    source "${PROJECT_ROOT}/.env"
    set +a
else
    error_message ".env file not found at: ${PROJECT_ROOT}/.env"
    exit 1
fi

# Load configuration
if [ -f "${PROJECT_ROOT}/config.sh" ]; then
    source "${PROJECT_ROOT}/config.sh"
else
    error_message "config.sh not found at: ${PROJECT_ROOT}/config.sh"
    exit 1
fi

# Check if required directories exist
if [ ! -d "/mnt/nfs/lefodata/data/drone_missions/" ]; then
    error_message "Source directory not found: /mnt/nfs/lefodata/data/drone_missions/"
    exit 1
fi

OUTPUT_DIR="${PROJECT_ROOT}/projects/$LABELBOX_PROJECT/output_maps"
SKIPPED_MISSIONS=()

# Loop through each mission
for MISSION in "${MISSIONS[@]}"; do
    log_message "Processing mission: $MISSION"

    # Check if mission directory exists
    YEAR="${MISSION:0:4}"
    if [ ! -d "/mnt/nfs/lefodata/data/drone_missions/$YEAR/$MISSION" ]; then
        error_message "Mission directory not found: /mnt/nfs/lefodata/data/drone_missions/$YEAR/$MISSION"
        continue
    fi

    source /opt/miniconda3/bin/activate labelbox
    check_command "conda activate labelbox"

    log_message "Checking pictures for $MISSION on lefodata"
    CHECK_STATUS=0
    python "${PROJECT_ROOT}/scripts/python/check_pictures.py" --mission "$MISSION" || CHECK_STATUS=$?
    # Exit code 10: picture mismatch, skip this mission; 11: drop unmatched pictures
    DROP_UNMATCHED_ARG=""
    if [ $CHECK_STATUS -eq 10 ]; then
        error_message "Mission $MISSION was not added to Labelbox (picture mismatch)"
        SKIPPED_MISSIONS+=("$MISSION")
        conda deactivate
        continue
    elif [ $CHECK_STATUS -eq 11 ]; then
        DROP_UNMATCHED_ARG="--drop-unmatched"
    elif [ $CHECK_STATUS -ne 0 ]; then
        error_message "check_pictures.py for $MISSION failed"
        exit 1
    fi

    log_message "Copying data from lefodata to Arbutus for $MISSION"
    PROJECT_BUCKET="${LABELBOX_BUCKET:-${LABELBOX_PROJECT//_/-}}"
    rclone --config "${PROJECT_ROOT}/rclone_newArbutus.conf" copy /mnt/nfs/lefodata/data/drone_missions/$YEAR/$MISSION/ ArbutusBuckets:$PROJECT_BUCKET/drone_missions/$YEAR/$MISSION -c
    check_command "rclone copy for $MISSION"
    log_message "Data copied to Arbutus for $MISSION"

    log_message "Importing data rows for $MISSION in Labelbox"
    if [ -n "$LABELBOX_PROJECT" ]; then
        LABELBOX_PROJECT_ARG="--project $LABELBOX_PROJECT"
    else
        LABELBOX_PROJECT_ARG=""
    fi
    IMPORT_STATUS=0
    python "${PROJECT_ROOT}/scripts/python/import_datarows.py" --mission "$MISSION" $LABELBOX_PROJECT_ARG --bucket "$PROJECT_BUCKET" $DROP_UNMATCHED_ARG || IMPORT_STATUS=$?
    # Exit code 10: picture mismatch, skip this mission
    if [ $IMPORT_STATUS -eq 10 ]; then
        error_message "Mission $MISSION was not added to Labelbox (picture mismatch)"
        SKIPPED_MISSIONS+=("$MISSION")
        conda deactivate
        continue
    elif [ $IMPORT_STATUS -ne 0 ]; then
        error_message "import_datarows.py for $MISSION failed"
        exit 1
    fi
    log_message "Data rows imported for $MISSION in Labelbox"

    # Skip maps if Arbutus already has one per close-up picture (tele, else legacy zoom)
    MISSION_REMOTE="ArbutusBuckets:$PROJECT_BUCKET/drone_missions/$YEAR/$MISSION"
    PICTURES=$(rclone --config "${PROJECT_ROOT}/rclone_newArbutus.conf" lsf -R --files-only "$MISSION_REMOTE" | grep -i '\.jpg$' || true)
    CLOSEUPS=$(grep -c 'tele' <<< "$PICTURES" || true)
    [ "$CLOSEUPS" -eq 0 ] && CLOSEUPS=$(grep -c 'zoom' <<< "$PICTURES" || true)
    MAPS=$(rclone --config "${PROJECT_ROOT}/rclone_newArbutus.conf" lsf --files-only "$MISSION_REMOTE/labelbox/attachments" 2>/dev/null | grep -c '\.html$' || true)
    if [ "$FORCE_MAPS" != true ] && [ "$CLOSEUPS" -gt 0 ] && [ "$MAPS" -eq "$CLOSEUPS" ]; then
        log_message "Maps already on Arbutus for $MISSION ($MAPS/$CLOSEUPS), skipping"
        MAPS_GENERATED=false
    else
        log_message "Generating maps for $MISSION ($MAPS/$CLOSEUPS on Arbutus)"
        # Mapping mission of the drone site (second segment of the mission name), else MAPPING_MISSION
        SITE=$(echo "$MISSION" | cut -d_ -f2)
        SITE_MAPPING_MISSION="${SITE_MAPPING_MISSIONS[$SITE]:-$MAPPING_MISSION}"
        if [ -n "$SITE_MAPPING_MISSION" ]; then
            MAPPING_MISSION_ARG="--mapping_mission $SITE_MAPPING_MISSION"
        else
            MAPPING_MISSION_ARG=""
        fi
        python "${PROJECT_ROOT}/scripts/python/generate_maps.py" --mission_id "$MISSION" --project_name "$LABELBOX_PROJECT" --bucket "$PROJECT_BUCKET" $MAPPING_MISSION_ARG
        check_command "generate_maps.py for $MISSION"
        log_message "Maps generated for $MISSION and saved in $OUTPUT_DIR"
        MAPS_GENERATED=true
    fi

    log_message "Sending data rows for $MISSION to $LABELBOX_PROJECT"
    python "${PROJECT_ROOT}/scripts/python/send_to_annotate.py" --mission_id "$MISSION" --project "$LABELBOX_PROJECT"
    check_command "send_to_annotate.py for $MISSION"
    log_message "Data rows sent for $MISSION to $LABELBOX_PROJECT"

    if [ "$PLANTNET_PREDICTIONS" = true ]; then
        log_message "Importing Pl@ntNet prelabels for $MISSION in $LABELBOX_PROJECT"
        python "${PROJECT_ROOT}/scripts/python/import_plantnet_predictions.py" --mission "$MISSION" --project "$LABELBOX_PROJECT"
        check_command "import_plantnet_predictions.py for $MISSION"
        log_message "Pl@ntNet prelabels imported for $MISSION"
    fi

    conda deactivate

    if [ "$MAPS_GENERATED" = true ]; then
        log_message "Copying maps to Arbutus for $MISSION"
        rclone --config "${PROJECT_ROOT}/rclone_newArbutus.conf" copy "$OUTPUT_DIR/$MISSION/" ArbutusBuckets:$PROJECT_BUCKET/drone_missions/$YEAR/$MISSION -c
        check_command "rclone copy maps for $MISSION"
        log_message "Maps copied to Arbutus for $MISSION"

        log_message "Cleaning up local maps for $MISSION"
        rm -rf "$OUTPUT_DIR/$MISSION"
        check_command "cleanup maps for $MISSION"
    fi

    log_message "Mission $MISSION completed successfully"

done

if [ ${#SKIPPED_MISSIONS[@]} -gt 0 ]; then
    error_message "Missions not added to Labelbox (picture mismatch): ${SKIPPED_MISSIONS[*]}"
fi

log_message "All missions completed successfully"

# ── PlantNet embeddings ────────────────────────────────────────────────────────
log_message "Getting and uploading PlantNet embeddings for $LABELBOX_PROJECT"
source /opt/miniconda3/bin/activate labelbox
python "${PROJECT_ROOT}/scripts/python/get_upload_embeddings.py" --project "$LABELBOX_PROJECT" \
    || log_message "WARNING: Embeddings step did not complete (daily quota may have been reached). Re-run tomorrow."
conda deactivate
log_message "Embeddings step finished for $LABELBOX_PROJECT"
