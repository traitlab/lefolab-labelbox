import argparse
import boto3
import geopandas as gpd
import logging
import os
import pandas as pd
import re
import sys

from botocore import UNSIGNED
from botocore.client import Config
from concurrent.futures import ThreadPoolExecutor, as_completed
from dotenv import load_dotenv
from pathlib import Path
from shapely.geometry import Point

from _common import get_coordinates_from_image_url, setup_session

# Setup logging with timestamp
logger = logging.getLogger()
logger.setLevel(logging.INFO)

_formatter = logging.Formatter('[%(asctime)s] %(message)s', datefmt='%Y-%m-%d %H:%M:%S')

_stdout_handler = logging.StreamHandler(sys.stdout)
_stdout_handler.setLevel(logging.INFO)
_stdout_handler.addFilter(lambda record: record.levelno == logging.INFO)
_stdout_handler.setFormatter(_formatter)

_stderr_handler = logging.StreamHandler(sys.stderr)
_stderr_handler.setLevel(logging.WARNING)
_stderr_handler.setFormatter(_formatter)

logger.handlers = []
logger.addHandler(_stdout_handler)
logger.addHandler(_stderr_handler)

# Load environment variables from .env file
project_root = Path(__file__).parent.parent.parent
load_dotenv(dotenv_path=project_root / '.env')

ALLIANCECAN_URL = os.getenv('ALLIANCECAN_URL')
AWS_ACCESS_KEY_ID = os.getenv('AWS_ACCESS_KEY_ID')
AWS_SECRET_ACCESS_KEY = os.getenv('AWS_SECRET_ACCESS_KEY')

if not ALLIANCECAN_URL:
    raise ValueError('ALLIANCECAN_URL environment variable is not set')
if not AWS_ACCESS_KEY_ID or not AWS_SECRET_ACCESS_KEY:
    logger.warning('AWS credentials not set. Assuming public bucket access.')

# The S3 API lives at the bare host; ALLIANCECAN_URL carries the Swift object
# path (/swift/v1/...) used to build public asset URLs.
s3_endpoint = ALLIANCECAN_URL.split('/swift/')[0]

if AWS_ACCESS_KEY_ID and AWS_SECRET_ACCESS_KEY:
    s3_client = boto3.client(
        's3',
        endpoint_url=s3_endpoint,
        aws_access_key_id=AWS_ACCESS_KEY_ID,
        aws_secret_access_key=AWS_SECRET_ACCESS_KEY,
        config=Config(signature_version='s3v4')
    )
else:
    s3_client = boto3.client(
        's3',
        endpoint_url=s3_endpoint,
        config=Config(signature_version=UNSIGNED)
    )

def _extract_identifier(filename):
    last_part = os.path.basename(filename).split('_')[-1].lower()
    return re.sub(r'(wide|med|tele|zoom)?\.jpg$', '', last_part)


def process_mission(mission, files, mission_url, existing_missions, existing_gdf, rows, max_workers):
    jpg_files = [f for f in files if f.lower().endswith('.jpg')]
    closeup_files = [f for f in jpg_files if 'tele' in f.lower() or 'zoom' in f.lower()]
    med_files = [f for f in jpg_files if 'med' in f.lower()]
    wide_files = [f for f in jpg_files if 'tele' not in f.lower() and 'zoom' not in f.lower() and 'med' not in f.lower()]

    wide_lookup = {_extract_identifier(f): f for f in wide_files}
    med_lookup = {_extract_identifier(f): f for f in med_files}

    if len(wide_files) != len(closeup_files):
        logger.warning(f"Mission '{mission}': Number of wide files ({len(wide_files)}) does not match number of close-up files ({len(closeup_files)})")
    if med_files and len(med_files) != len(closeup_files):
        logger.warning(f"Mission '{mission}': Number of med files ({len(med_files)}) does not match number of close-up files ({len(closeup_files)})")

    rows_before = len(rows)
    n_closeup = len(closeup_files)

    if mission in existing_missions:
        # A point is identified by its mission and point id
        existing_points = set()
        if existing_gdf is not None:
            existing_points = set(existing_gdf[existing_gdf['mission_id'] == mission]['point_id'])
        closeup_files_to_add = [f for f in closeup_files if _extract_identifier(f) not in existing_points]
        if not closeup_files_to_add:
            logger.info(f"All points for mission '{mission}' are already present. Skipping.")
            return
        logger.info(f"Adding {len(closeup_files_to_add)} missing points for mission '{mission}'.")
        closeup_files = closeup_files_to_add

    args_list = [(closeup_file, wide_lookup, med_lookup, mission, mission_url) for closeup_file in closeup_files]
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        results = list(executor.map(process_closeup_file, args_list))
    for result in results:
        if result:
            rows.append(result)

    rows_added = len(rows) - rows_before
    logger.info(f"Finished mission '{mission}': {rows_added} points added, {len(wide_files)} wide images, {n_closeup} close-up images.")


def process_closeup_file(args):
    session = setup_session()

    closeup_file, wide_lookup, med_lookup, mission, mission_url = args
    identifier = _extract_identifier(closeup_file)

    wide_file = wide_lookup.get(identifier)
    if not wide_file:
        logger.warning(f"Could not find matching wide photo for {closeup_file} with identifier {identifier}")
        return None

    med_file = med_lookup.get(identifier)

    wide_url = f"{mission_url}/{wide_file}"
    med_url = f"{mission_url}/{med_file}" if med_file else None
    tele_url = f"{mission_url}/{closeup_file}"
    coords = get_coordinates_from_image_url(wide_url, session)
    if coords:
        return {
            'geometry': Point(coords[1], coords[0]),
            'mission_id': mission,
            'point_id': identifier,
            'wide_url': wide_url,
            'med_url': med_url,
            'tele_url': tele_url
        }
    return None

def list_prefixes(bucket, prefix):
    """List the immediate sub-prefixes of a prefix in a bucket."""
    pages = s3_client.get_paginator('list_objects_v2').paginate(Bucket=bucket, Prefix=prefix, Delimiter='/')
    return [cp['Prefix'] for page in pages for cp in page.get('CommonPrefixes', [])]


def main(output_dir, points_layer, project_buckets, max_workers=8):
    os.makedirs(output_dir, exist_ok=True)
    points_layer_path = os.path.join(output_dir, points_layer)
    existing_gdf = None
    existing_missions = set()
    existing_counts = dict()
    if os.path.exists(points_layer_path):
        existing_gdf = gpd.read_file(points_layer_path)
        if 'mission_id' in existing_gdf.columns:
            existing_missions = set(existing_gdf['mission_id'].unique())
            # Count points per mission
            existing_counts = existing_gdf.groupby('mission_id').size().to_dict()
            logger.info("Existing point counts per mission:")
            for mission, count in existing_counts.items():
                logger.info(f"  {mission}: {count}")
        else:
            logger.warning("Existing points layer does not have 'mission_id' column. Skipping mission check.")
    else:
        logger.info("No existing points layer found. Will create a new one.")

    rows = []

    # Missions live at <project-bucket>/drone_missions/<yyyy>/<mission>/
    # Bucket names allow no underscore, so project names are given either way
    for bucket in [b.replace('_', '-') for b in project_buckets]:
        mission_prefixes = [p for year in list_prefixes(bucket, 'drone_missions/')
                            for p in list_prefixes(bucket, year)]
        wpt_prefixes = [p for p in mission_prefixes if 'wpt' in p.lower()]
        logger.info(f"Found {len(wpt_prefixes)} waypoint mission(s) in bucket '{bucket}' (out of {len(mission_prefixes)}).")

        for prefix in wpt_prefixes:
            mission = prefix.rstrip('/').split('/')[-1]
            mission_url = f"{ALLIANCECAN_URL}/{bucket}/{prefix.rstrip('/')}"
            paginator = s3_client.get_paginator('list_objects_v2')
            pages = paginator.paginate(Bucket=bucket, Prefix=prefix)
            files = []
            for page in pages:
                for obj in page.get('Contents', []):
                    rel = obj['Key'][len(prefix):]
                    if rel:
                        files.append(rel)
            process_mission(mission, files, mission_url, existing_missions, existing_gdf, rows, max_workers)

    # After processing all folders
    if rows:
        if existing_gdf is not None and not existing_gdf.empty:
            # Concatenate existing and new rows, avoiding duplicates
            new_gdf = gpd.GeoDataFrame(rows, crs='EPSG:4326')
            combined_gdf = pd.concat([existing_gdf, new_gdf], ignore_index=True)
            # Drop duplicates based on mission_id, point_id, and wide_url
            combined_gdf = combined_gdf.drop_duplicates(subset=['mission_id', 'point_id', 'wide_url'])
            combined_gdf.to_file(points_layer_path, driver="GPKG")
        else:
            gdf = gpd.GeoDataFrame(rows, crs='EPSG:4326')
            gdf.to_file(points_layer_path, driver="GPKG")
        logger.info(f"Successfully saved {len(rows)} new points to {points_layer_path}")
    else:
        logger.info("No new points to add. Points layer unchanged.")

    # Projected copy in the local UTM zone, named with its EPSG code
    if os.path.exists(points_layer_path):
        gdf = gpd.read_file(points_layer_path)
        utm_crs = gdf.estimate_utm_crs()
        utm_path = points_layer_path.replace('.gpkg', f'_{utm_crs.to_epsg()}.gpkg')
        gdf.to_crs(utm_crs).to_file(utm_path, driver="GPKG")
        logger.info(f"Saved projected copy ({utm_crs.name}) to {utm_path}")

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Build a waypoint points layer from the Arbutus project buckets.")
    parser.add_argument("--output_dir", required=True, help="Output directory for logs and points layer.")
    parser.add_argument("--project_bucket", required=True, nargs="+", help="One or more Arbutus project buckets (e.g. 2024_bci). All their waypoint missions are included.")
    parser.add_argument("--max_workers", type=int, default=8, help="Number of parallel workers.")
    parser.add_argument("--points_layer", required=False, help="Points layer filename to use or create. Defaults to '<project_bucket>_wpt.gpkg'. Required when multiple buckets are given.")
    args = parser.parse_args()

    # Set default points_layer if not provided
    if args.points_layer:
        points_layer = args.points_layer
    elif len(args.project_bucket) == 1:
        points_layer = f"{args.project_bucket[0].replace('-', '_')}_wpt.gpkg"
    else:
        parser.error("--points_layer is required when multiple project buckets are given.")

    main(
        output_dir=args.output_dir,
        points_layer=points_layer,
        project_buckets=args.project_bucket,
        max_workers=args.max_workers
    )
