#!/bin/bash

# Transfer drone missions from lefodata (the reference) to the new Arbutus.
#
# Every waypoint mission on lefodata whose name contains <qualifier> (case-insensitive)
# is copied to <project> on the new Arbutus. Only names containing wpt are considered,
# so mapping missions are left out:
#
#   $LEFODATA_PATH/drone_missions/<yyyy>/<mission>/
#     -> ArbutusBuckets:<project with underscores as hyphens>/drone_missions/<yyyy>/<mission>/
#
# Missions already present and complete on the new Arbutus are skipped: the target
# path is listed first and compared to lefodata on file count and total bytes. The
# derived labelbox/ folder is excluded from that comparison because it is produced
# by this repo and never exists on lefodata.
#
# A mission already held under a DIFFERENT project bucket is reported and skipped,
# so it is not silently duplicated across two buckets.
#
# Each copy is verified with `rclone check --one-way` (every lefodata file must
# exist identically on the new Arbutus). Nothing is deleted and nothing is written
# to lefodata, so the script is safe to re-run.
#
# Usage: ./transfer_missions_to_arbutus.sh [-y] <qualifier> <project>
#        ./transfer_missions_to_arbutus.sh bciarmour 2024_bci
#        ./transfer_missions_to_arbutus.sh -f <file> [-y]
#
# -y copies without asking, so the script can be left running under nohup:
#   nohup ./transfer_missions_to_arbutus.sh -f missions.txt -y > transfer.log 2>&1 &
#
# The -f file names the missions explicitly, one per line, with the mission and its
# project bucket separated by a tab. Blank lines and lines starting with # are ignored.
# A mission listed there is transferred as named, without the wpt filter:
#
#   20260114_bciarmour_wptne01_m3e<TAB>2024-bci
#   20260704_maneneenfr_wpt_m3e<TAB>2026-darien

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

MANIFEST=""
ASSUME_YES=""
while [ $# -gt 0 ]; do
    case "$1" in
        -f)
            MANIFEST="$2"
            if [ -z "$MANIFEST" ]; then
                error_message "-f needs a file (tab-separated: mission<TAB>project_bucket)"
                exit 1
            fi
            shift 2
            ;;
        -y)
            ASSUME_YES=1
            shift
            ;;
        *)
            break
            ;;
    esac
done

if [ -n "$MANIFEST" ]; then
    if [ ! -f "$MANIFEST" ]; then
        error_message "File not found: $MANIFEST"
        exit 1
    fi
else
    QUALIFIER="$1"
    PROJECT="$2"
    if [ -z "$QUALIFIER" ] || [ -z "$PROJECT" ]; then
        error_message "Usage: $0 [-y] <qualifier> <project> (e.g. $0 wpt 2026_darien) or $0 -f <file> [-y]"
        exit 1
    fi
fi

# Get the project root directory
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "${SCRIPT_DIR}/../.." && pwd)"

# Load environment variables from .env file (LEFODATA_PATH)
if [ -f "${PROJECT_ROOT}/.env" ]; then
    set -a
    source "${PROJECT_ROOT}/.env"
    set +a
else
    error_message ".env file not found at: ${PROJECT_ROOT}/.env"
    exit 1
fi

if [ -z "$LEFODATA_PATH" ]; then
    error_message "LEFODATA_PATH is not set in .env"
    exit 1
fi

LEFODATA_ROOT="${LEFODATA_PATH%/}/drone_missions"
if [ ! -d "$LEFODATA_ROOT" ]; then
    error_message "lefodata directory not found: $LEFODATA_ROOT"
    exit 1
fi

NEW_CONF="${PROJECT_ROOT}/rclone_newArbutus.conf"
NEW_REMOTE="ArbutusBuckets"
if [ ! -f "$NEW_CONF" ]; then
    error_message "rclone config not found: $NEW_CONF"
    exit 1
fi

# Strict S3 bucket naming on the new Arbutus: no underscores allowed
PROJECT_BUCKET="${PROJECT//_/-}"

# Index every mission already on the new Arbutus, to spot the ones held under
# another project bucket
log_message "Indexing the missions already on the new Arbutus"
declare -A MISSION_BUCKET
for BUCKET in $(rclone --config "$NEW_CONF" lsf --dirs-only "${NEW_REMOTE}:"); do
    BUCKET="${BUCKET%/}"
    while IFS= read -r DIR; do
        MISSION_BUCKET["$(basename "$DIR")"]="$BUCKET"
    done < <(rclone --config "$NEW_CONF" lsf -R --dirs-only --max-depth 2 \
                 "${NEW_REMOTE}:${BUCKET}/drone_missions" 2>/dev/null | awk -F/ 'NF==3')
done
log_message "${#MISSION_BUCKET[@]} missions already on the new Arbutus"

# The missions to consider, and the bucket each one goes to
CANDIDATES=()
CANDIDATE_YEARS=()
CANDIDATE_BUCKETS=()
UNKNOWN=()

if [ -n "$MANIFEST" ]; then
    log_message "Reading the missions listed in $MANIFEST"
    while IFS=$'\t' read -r MISSION BUCKET _ || [ -n "$MISSION" ]; do
        case "$MISSION" in ''|'#'*) continue ;; esac
        # A file exported from a spreadsheet can carry CRLF endings
        BUCKET="${BUCKET%$'\r'}"
        if [ -z "$BUCKET" ]; then
            error_message "No project bucket given for $MISSION in $MANIFEST"
            exit 1
        fi

        YEAR=""
        for YEAR_DIR in "$LEFODATA_ROOT"/*/; do
            if [ -d "${YEAR_DIR}${MISSION}" ]; then
                YEAR="$(basename "$YEAR_DIR")"
                break
            fi
        done
        if [ -z "$YEAR" ]; then
            UNKNOWN+=("$MISSION")
            continue
        fi

        CANDIDATES+=("$MISSION")
        CANDIDATE_YEARS+=("$YEAR")
        CANDIDATE_BUCKETS+=("${BUCKET//_/-}")
    done < "$MANIFEST"
else
    log_message "Looking for lefodata wpt missions matching '$QUALIFIER'"
    shopt -s nocasematch nullglob
    for YEAR_DIR in "$LEFODATA_ROOT"/*/; do
        YEAR="$(basename "$YEAR_DIR")"
        for MISSION_DIR in "$YEAR_DIR"*/; do
            MISSION="$(basename "$MISSION_DIR")"
            # Waypoint missions only, further narrowed by the qualifier
            [[ "$MISSION" == *wpt* && "$MISSION" == *"$QUALIFIER"* ]] || continue
            CANDIDATES+=("$MISSION")
            CANDIDATE_YEARS+=("$YEAR")
            CANDIDATE_BUCKETS+=("$PROJECT_BUCKET")
        done
    done
    shopt -u nocasematch nullglob
fi

# Compare each candidate with its target path before copying anything
TO_TRANSFER=()
TRANSFER_YEARS=()
TRANSFER_BUCKETS=()
SKIPPED=()
ELSEWHERE=()

for INDEX in "${!CANDIDATES[@]}"; do
    MISSION="${CANDIDATES[$INDEX]}"
    YEAR="${CANDIDATE_YEARS[$INDEX]}"
    BUCKET="${CANDIDATE_BUCKETS[$INDEX]}"
    MISSION_DIR="${LEFODATA_ROOT}/${YEAR}/${MISSION}/"
    DST="${NEW_REMOTE}:${BUCKET}/drone_missions/${YEAR}/${MISSION}"

    read -r SRC_FILES SRC_BYTES < <(
        find "$MISSION_DIR" -type f -printf '%s\n' |
            awk '{n++; s+=$1} END {printf "%d %.0f\n", n, s}'
    )
    DST_SIZE=$(rclone --config "$NEW_CONF" size --json --exclude "labelbox/**" "$DST" 2>/dev/null || echo '{"count":0,"bytes":0}')
    DST_FILES=$(echo "$DST_SIZE" | grep -o '"count":[0-9]*' | cut -d: -f2)
    DST_BYTES=$(echo "$DST_SIZE" | grep -o '"bytes":[0-9]*' | cut -d: -f2)

    if [ "$DST_FILES" -eq "$SRC_FILES" ] && [ "$DST_BYTES" -eq "$SRC_BYTES" ]; then
        SKIPPED+=("$MISSION")
        continue
    fi

    # Only a target holding nothing counts as a duplicate: partial data there
    # means the mission was meant for this project after all
    OTHER_BUCKET="${MISSION_BUCKET[$MISSION]}"
    if [ -n "$OTHER_BUCKET" ] && [ "$OTHER_BUCKET" != "$BUCKET" ] && [ "$DST_FILES" -eq 0 ]; then
        ELSEWHERE+=("$MISSION (in $OTHER_BUCKET)")
        continue
    fi

    TO_TRANSFER+=("$MISSION")
    TRANSFER_YEARS+=("$YEAR")
    TRANSFER_BUCKETS+=("$BUCKET")
done

if [ ${#UNKNOWN[@]} -gt 0 ]; then
    log_message "WARNING: ${#UNKNOWN[@]} listed missions are not on lefodata, skipping:"
    printf '  %s\n' "${UNKNOWN[@]}"
fi

if [ ${#SKIPPED[@]} -gt 0 ]; then
    log_message "${#SKIPPED[@]} missions already complete on their target bucket, skipping:"
    printf '  %s\n' "${SKIPPED[@]}"
fi

if [ ${#ELSEWHERE[@]} -gt 0 ]; then
    log_message "WARNING: ${#ELSEWHERE[@]} missions are already on the new Arbutus under another project, skipping:"
    printf '  %s\n' "${ELSEWHERE[@]}"
    log_message "Re-run with that project as <project> if they belong there."
fi

if [ ${#TO_TRANSFER[@]} -eq 0 ]; then
    log_message "Nothing to transfer"
    exit 0
fi

log_message "${#TO_TRANSFER[@]} missions to transfer:"
for INDEX in "${!TO_TRANSFER[@]}"; do
    printf '  %s -> %s\n' "${TO_TRANSFER[$INDEX]}" "${TRANSFER_BUCKETS[$INDEX]}"
done

if [ -z "$ASSUME_YES" ]; then
    read -r -p "Copy these ${#TO_TRANSFER[@]} missions from lefodata to the new Arbutus? [y/N] " REPLY
    if [[ ! "$REPLY" =~ ^[Yy]$ ]]; then
        log_message "Aborted, nothing copied"
        exit 0
    fi
fi

FAILED=()
for INDEX in "${!TO_TRANSFER[@]}"; do
    MISSION="${TO_TRANSFER[$INDEX]}"
    YEAR="${TRANSFER_YEARS[$INDEX]}"
    BUCKET="${TRANSFER_BUCKETS[$INDEX]}"
    SRC="${LEFODATA_ROOT}/${YEAR}/${MISSION}"
    DST="${NEW_REMOTE}:${BUCKET}/drone_missions/${YEAR}/${MISSION}"

    log_message "Copying $MISSION to $BUCKET"
    if ! rclone --config "$NEW_CONF" copy "$SRC/" "$DST" -c; then
        error_message "rclone copy failed for $MISSION"
        FAILED+=("$MISSION")
        continue
    fi

    log_message "Verifying $MISSION"
    if rclone --config "$NEW_CONF" check "$SRC/" "$DST" --one-way; then
        log_message "Mission $MISSION transferred and verified"
    else
        error_message "Verification failed for $MISSION"
        FAILED+=("$MISSION")
    fi
done

echo
if [ ${#FAILED[@]} -gt 0 ]; then
    error_message "${#FAILED[@]} of ${#TO_TRANSFER[@]} missions FAILED:"
    printf '  %s\n' "${FAILED[@]}" >&2
    error_message "Re-run this script to retry: the missions that succeeded are skipped."
    exit 1
fi

log_message "All ${#TO_TRANSFER[@]} missions transferred to the new Arbutus and verified"
log_message "Run compare_wpt_missions.py --refresh new to update the comparison report."
