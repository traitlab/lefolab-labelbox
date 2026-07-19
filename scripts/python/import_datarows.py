import argparse
import boto3
import copy
import logging
import os
import sys

from botocore import UNSIGNED
from botocore.client import Config

from _common import get_client, setup_logging

logger = setup_logging()

# Suppress urllib3 connection pool warnings
logging.getLogger('urllib3.connectionpool').setLevel(logging.ERROR)

# Get environment variables
ALLIANCECAN_URL = os.getenv("ALLIANCECAN_URL")
AWS_ACCESS_KEY_ID = os.getenv("AWS_ACCESS_KEY_ID")
AWS_SECRET_ACCESS_KEY = os.getenv("AWS_SECRET_ACCESS_KEY")

# Verify environment variables are set
if not ALLIANCECAN_URL:
    logger.error("ALLIANCECAN_URL environment variable is not set")
    raise ValueError("ALLIANCECAN_URL environment variable is not set")
if not AWS_ACCESS_KEY_ID or not AWS_SECRET_ACCESS_KEY:
    logger.warning("AWS_ACCESS_KEY_ID and/or AWS_SECRET_ACCESS_KEY environment variables are not set. Assuming public bucket access.")

client = get_client()

# Parse command-line arguments
parser = argparse.ArgumentParser(description="Import data rows into Labelbox.")
parser.add_argument("--mission", required=True, help="Mission ID of the pictures to import.")
parser.add_argument("--project", help="Project ID of the mission. Based on Fulcrum project and used for dataset name")
args = parser.parse_args()

mission = args.mission

if args.project:
    project = args.project
else:
    parts = mission.split('_')
    if len(parts) >= 4:
        site = parts[1]
        if site.startswith('tbs'):
            project = '2025_tiputini'
        elif site.startswith('bci'):
            project = '2024_bci'
        else:
            logger.error("Site in mission name is not recognized, unable to extract project for Labelbox dataset.")
            raise ValueError("Site in mission name is not recognized, unable to extract project for Labelbox dataset. Please provide a project.")
    else:
        logger.error("Mission name does not follow expected format, unable to extract project for Labelbox dataset.")
        raise ValueError("Mission name does not follow expected format, unable to extract project for Labelbox dataset. Please provide a project.")

# List all pictures on Alliance Canada for a given mission

# Per-project bucket (no underscores allowed) and object prefix on Arbutus:
# <project-with-hyphens>/drone_missions/<yyyy>/<mission>/...
project_bucket = project.replace("_", "-")
year = mission[:4]
prefix = f"drone_missions/{year}/{mission}"

# The S3 API lives at the bare host; ALLIANCECAN_URL also carries the Swift
# object path (/swift/v1/...) used to build public asset URLs (folder_url).
# Strip the Swift path for the boto3 endpoint.
s3_endpoint = ALLIANCECAN_URL.split('/swift/')[0]

# Configure S3 client for Alliance Canada (S3-compatible storage)
if AWS_ACCESS_KEY_ID and AWS_SECRET_ACCESS_KEY:
    # Use credentials if provided
    s3_client = boto3.client(
        's3',
        endpoint_url=s3_endpoint,
        aws_access_key_id=AWS_ACCESS_KEY_ID,
        aws_secret_access_key=AWS_SECRET_ACCESS_KEY,
        config=Config(signature_version='s3v4')
    )
else:
    # Use anonymous access (public bucket)
    s3_client = boto3.client(
        's3',
        endpoint_url=s3_endpoint,
        config=Config(signature_version=UNSIGNED)
    )

# Use paginator to automatically handle pagination
try:
    paginator = s3_client.get_paginator('list_objects_v2')
    pages = paginator.paginate(Bucket=project_bucket, Prefix=prefix)

    # Collect all file keys
    file_keys = []
    for page in pages:
        if 'Contents' in page:
            for obj in page['Contents']:
                key = obj['Key']
                if key.lower().endswith('.jpg'):
                    file_keys.append(key)
except Exception as e:
    logger.error(f"Failed to retrieve files from S3 bucket: {e}")
    sys.exit(1)

# Construct folder URL for generating asset URLs
folder_url = f"{ALLIANCECAN_URL}/{project_bucket}"

# Print the result
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

if len(wide_files_all) != len(closeup_files):
    logger.error(f"Picture count mismatch: {len(closeup_files)} close-up vs {len(wide_files_all)} wide pictures.")
    sys.exit(1)

if med_files_all:
    logger.info(f"{len(med_files_all)} med pictures found for this mission : {mission}")
    if len(med_files_all) != len(closeup_files):
        logger.error(f"Picture count mismatch: {len(closeup_files)} close-up vs {len(med_files_all)} med pictures.")
        sys.exit(1)

# Import data rows into Labelbox dataset named after the project
# Check if the dataset already exists
existing_datasets = client.get_datasets()
dataset_name = project
existing_dataset = next((ds for ds in existing_datasets if ds.name == dataset_name), None)

if existing_dataset:
    logger.info(f"Dataset {dataset_name} already exists. Importing data rows into this dataset.")
    dataset = existing_dataset
else:
    logger.info(f"Creating new dataset {dataset_name}")
    dataset = client.create_dataset(name=dataset_name)

# Base asset template
assets_template = {
    "row_data": "",
    "global_key": "",
    "media_type": "IMAGE",
    "metadata_fields": [{"name": "mission", "value": ""},
                        {"name": "polygon", "value": ""}],
    "attachments": [{"type": "IMAGE", "value": "", "name": "wide"},
                    {"type": "HTML", "value": "", "name": "map"}]
}

# Create a list of assets
assets = []

for i, closeup_file in enumerate(closeup_files):
    # Make a copy of the template for each asset
    asset = copy.deepcopy(assets_template)
    
    # Replace row_data with the current closeup_file (URL)
    asset["row_data"] = f"{folder_url}/{closeup_file}"
    
    # Use file name as unique global_key
    # Strip the mission prefix so the key stays <folder>/<image> as before,
    # not the full drone_missions/<year>/<mission>/<folder>/<image> path.
    file = closeup_file[len(prefix) + 1:] if closeup_file.startswith(prefix + "/") else closeup_file.split('/', 1)[-1]
    asset["global_key"] = file
    
    # Metadata fields : mission
    asset["metadata_fields"][0]["value"] = f"{mission}" 
    
    # Extract the polygon id and determine matching file suffixes
    if exclusion_keyword == "tele":
        polygon_id = closeup_file.split('_')[-1].lower().replace('tele.jpg', '')
        wide_file_end = f"_{polygon_id}wide.JPG"
    else:
        # Legacy naming convention (zoom)
        polygon_id = closeup_file.split('_')[-1].lower().replace('zoom.jpg', '')
        wide_file_end = f"_{polygon_id}.JPG"
    
    # Metadata fields : polygon_id
    asset["metadata_fields"][1]["value"] = f"{polygon_id}" 
    
    # Attach the map
    closeup_basename = os.path.basename(closeup_file)
    map_url = f"{folder_url}/{prefix}/labelbox/attachments/{closeup_basename.replace('.JPG', '.html')}"
    
    asset["attachments"][1]["value"] = map_url
    
    # Find the corresponding wide file from file_keys
    wide_file = None
    matching_wide_files = [key for key in file_keys if wide_file_end in key and exclusion_keyword not in key]
    
    if len(matching_wide_files) > 1:
        logger.error(f"Multiple wide pictures found for {closeup_file}: {matching_wide_files}. Exiting.")
        sys.exit(1)

    wide_file = matching_wide_files[0] if matching_wide_files else None
    
    # If a wide file is found, set the attachment value
    if wide_file:
        asset["attachments"][0]["value"] = f"{folder_url}/{wide_file}"
    else:
        logger.error(f"No wide file found for {closeup_file}. Exiting.")
        sys.exit(1)

    # Find the corresponding med file (optional)
    med_file_end = f"_{polygon_id}med.JPG"
    matching_med_files = [key for key in file_keys if med_file_end in key]

    if len(matching_med_files) > 1:
        logger.error(f"Multiple med pictures found for {closeup_file}: {matching_med_files}. Exiting.")
        sys.exit(1)

    med_file = matching_med_files[0] if matching_med_files else None

    if med_file:
        asset["attachments"].insert(1, {"type": "IMAGE", "value": f"{folder_url}/{med_file}", "name": "med"})

    # Add the updated asset to the list
    assets.append(asset)

# Import data in Labelbox
if not assets:
    logger.error("No valid assets to upload. Exiting.")
    sys.exit(1)
else:
    task = dataset.create_data_rows(assets)
    task.wait_till_done()
    errors = task.errors

    if errors is None:
        logger.info("No errors while importing data to Labelbox.")
    else:
        # Count duplicate global key errors
        duplicate_count = sum(1 for e in errors if e.get('message', '').startswith("Duplicate global key"))
        other_errors = [e for e in errors if not e.get('message', '').startswith("Duplicate global key")]

        if duplicate_count:
            logger.warning(f"{duplicate_count} duplicate global key errors.")

        if other_errors:
            logger.error(f"Other errors: {other_errors}")
        else:
            logger.info("No errors while importing data to Labelbox.")
