#!/bin/bash

# Migrate the pictures of one project from the legacy Arbutus bucket
# ($BUCKET_WPT/<mission>) to the new Arbutus
# (<project with underscores as hyphens>/drone_missions/<yyyy>/<mission>).
#
# The mission list is taken from the Labelbox dataset named after the project,
# so exactly what Labelbox references gets migrated. Each mission is copied
# then verified with `rclone check --one-way` (every legacy file must exist
# identically on the new Arbutus). Nothing is deleted from the legacy bucket.
#
# Safe to re-run: rclone copy only transfers missing/changed files, and the
# verification runs again. Usage: ./migrate_arbutus_buckets.sh <project>

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

PROJECT="$1"
if [ -z "$PROJECT" ]; then
    error_message "Usage: $0 <project> (e.g. $0 2025_wa_roberge)"
    exit 1
fi

# Get the project root directory
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"

# Load environment variables from .env file (AWS keys for the legacy remote,
# LABELBOX_API_KEY, BUCKET_WPT)
if [ -f "${PROJECT_ROOT}/.env" ]; then
    set -a
    source "${PROJECT_ROOT}/.env"
    set +a
else
    error_message ".env file not found at: ${PROJECT_ROOT}/.env"
    exit 1
fi

if [ -z "$BUCKET_WPT" ]; then
    error_message "BUCKET_WPT is not set in .env"
    exit 1
fi

LEGACY_CONF="/etc/rclone.conf"
NEW_CONF="${PROJECT_ROOT}/rclone_newArbutus.conf"
for conf in "$LEGACY_CONF" "$NEW_CONF"; do
    if [ ! -f "$conf" ]; then
        error_message "rclone config not found: $conf"
        exit 1
    fi
done

# The legacy remote (AllianceCanBuckets) and the new remote (ArbutusBuckets)
# live in separate config files: merge them so one rclone command can see both
MERGED_CONF=$(mktemp)
chmod 600 "$MERGED_CONF"
trap 'rm -f "$MERGED_CONF"' EXIT
cat "$LEGACY_CONF" "$NEW_CONF" > "$MERGED_CONF"

source /opt/miniconda3/bin/activate labelbox

log_message "Listing missions of project $PROJECT from Labelbox"
mapfile -t MISSIONS < <(python "${SCRIPT_DIR}/list_project_missions.py" --project "$PROJECT")

if [ ${#MISSIONS[@]} -eq 0 ]; then
    error_message "No missions returned for project $PROJECT"
    exit 1
fi

# Strict S3 bucket naming on the new Arbutus: no underscores allowed
NEW_BUCKET="${PROJECT//_/-}"

log_message "Missions to migrate to bucket $NEW_BUCKET on the new Arbutus:"
printf '  %s\n' "${MISSIONS[@]}"

read -r -p "Copy these ${#MISSIONS[@]} missions from ${BUCKET_WPT} (legacy) to ${NEW_BUCKET} (new)? [y/N] " REPLY
if [[ ! "$REPLY" =~ ^[Yy]$ ]]; then
    log_message "Aborted, nothing copied"
    exit 0
fi

FAILED=()
for MISSION in "${MISSIONS[@]}"; do
    YEAR="${MISSION:0:4}"
    SRC="AllianceCanBuckets:${BUCKET_WPT}/${MISSION}"
    DST="ArbutusBuckets:${NEW_BUCKET}/drone_missions/${YEAR}/${MISSION}"

    log_message "Copying $MISSION to new Arbutus"
    if ! rclone --config "$MERGED_CONF" copy "$SRC" "$DST" -c; then
        error_message "rclone copy failed for $MISSION"
        FAILED+=("$MISSION")
        continue
    fi

    log_message "Verifying $MISSION"
    if rclone --config "$MERGED_CONF" check "$SRC" "$DST" --one-way; then
        log_message "Mission $MISSION transferred and verified"
    else
        error_message "Verification failed for $MISSION"
        FAILED+=("$MISSION")
    fi
done

echo
if [ ${#FAILED[@]} -gt 0 ]; then
    error_message "${#FAILED[@]} of ${#MISSIONS[@]} missions FAILED:"
    printf '  %s\n' "${FAILED[@]}" >&2
    error_message "Re-run this script to retry. Do NOT delete anything from the legacy bucket."
    exit 1
fi

log_message "All ${#MISSIONS[@]} missions of $PROJECT transferred and verified on the new Arbutus"
log_message "Legacy bucket untouched. Next steps: update the Labelbox URLs, then delete the legacy data."
