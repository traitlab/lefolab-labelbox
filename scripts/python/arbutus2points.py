import argparse
import boto3
import exifread
import geopandas as gpd
import logging
import os
import pandas as pd
import requests
import re
import sys
import time

from botocore import UNSIGNED
from botocore.client import Config
from contextlib import redirect_stderr
from concurrent.futures import ThreadPoolExecutor, as_completed
from dotenv import load_dotenv
from io import BytesIO
from io import StringIO
from pathlib import Path
from requests.adapters import HTTPAdapter
from shapely.geometry import Point
from urllib3.util.retry import Retry

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
project_root = Path(__file__).parent.parent
load_dotenv(dotenv_path=project_root / '.env')

ALLIANCECAN_URL = os.getenv('ALLIANCECAN_URL')
BUCKET_WPT = os.getenv('BUCKET_WPT')
AWS_ACCESS_KEY_ID = os.getenv('AWS_ACCESS_KEY_ID')
AWS_SECRET_ACCESS_KEY = os.getenv('AWS_SECRET_ACCESS_KEY')

if not ALLIANCECAN_URL:
    raise ValueError('ALLIANCECAN_URL environment variable is not set')
if not BUCKET_WPT:
    raise ValueError('BUCKET_WPT environment variable is not set')
if not AWS_ACCESS_KEY_ID or not AWS_SECRET_ACCESS_KEY:
    logger.warning('AWS credentials not set. Assuming public bucket access.')

if AWS_ACCESS_KEY_ID and AWS_SECRET_ACCESS_KEY:
    s3_client = boto3.client(
        's3',
        endpoint_url=ALLIANCECAN_URL,
        aws_access_key_id=AWS_ACCESS_KEY_ID,
        aws_secret_access_key=AWS_SECRET_ACCESS_KEY,
        config=Config(signature_version='s3v4')
    )
else:
    s3_client = boto3.client(
        's3',
        endpoint_url=ALLIANCECAN_URL,
        config=Config(signature_version=UNSIGNED)
    )

def convert_to_decimal_degrees(value, ref):
    """
    Convert GPS coordinates to decimal degrees.
    
    Args:
        value: GPS coordinate value.
        ref: GPS coordinate reference (N, S, E, W).
        
    Returns:
        float: Coordinate in decimal degrees.
    """
    if len(value.values) != 3:
        raise ValueError("Malformed or incomplete EXIF data: GPS coordinate value does not contain exactly three elements")
    d, m, s = [float(x.num) / float(x.den) for x in value.values]
    decimal_degrees = d + (m / 60) + (s / 3600)
    if ref.values and ref.values[0] in ['S', 'W']:
        decimal_degrees = -decimal_degrees
    return decimal_degrees

def setup_session():
    """Create a requests session with retry strategy"""
    session = requests.Session()
    retry_strategy = Retry(
        total=3,
        status_forcelist=[429, 500, 502, 503, 504],
        allowed_methods=["HEAD", "GET", "OPTIONS"],
        backoff_factor=1
    )
    adapter = HTTPAdapter(max_retries=retry_strategy)
    session.mount("http://", adapter)
    session.mount("https://", adapter)
    return session

def safe_request(session, url, timeout=30, max_retries=3):
    """Make a safe HTTP request with error handling"""
    for attempt in range(max_retries):
        try:
            response = session.get(url, timeout=timeout)
            return response
        except (requests.exceptions.ConnectionError, 
                requests.exceptions.Timeout,
                requests.exceptions.RequestException) as e:
            logger.error(f"Request failed (attempt {attempt + 1}/{max_retries}): {e}")
            if attempt < max_retries - 1:
                time.sleep(2 ** attempt)  # Exponential backoff
            else:
                logger.error(f"Failed to fetch {url} after {max_retries} attempts")
                return None

def get_coordinates_from_image_url(picture_url, session):
    """
    Get latitude and longitude from the image metadata.
    
    Args:
        picture_url (str): URL of the image to process.
        session: Requests session with retry logic.
        
    Returns:
        tuple or None: (latitude, longitude) in decimal degrees if found, otherwise None.
    """
    response = safe_request(session, picture_url)

    if response and response.status_code == 200:
        # Load the image into BytesIO
        image_data = BytesIO(response.content)
        
        try:
            with redirect_stderr(StringIO()):
                tags = exifread.process_file(image_data, details=False)
        except (IndexError, KeyError, ValueError) as e:
            logger.error(f"Error processing EXIF data: {e} for {picture_url}")
            return None
        
        latitude = tags.get('GPS GPSLatitude')
        latitude_ref = tags.get('GPS GPSLatitudeRef')
        longitude = tags.get('GPS GPSLongitude')
        longitude_ref = tags.get('GPS GPSLongitudeRef')
        
        # Check if EXIF tags are present
        if latitude and latitude_ref and longitude and longitude_ref:
            try:
                # Convert to decimal degrees
                latitude = convert_to_decimal_degrees(latitude, latitude_ref)
                longitude = convert_to_decimal_degrees(longitude, longitude_ref)
                return latitude, longitude
            
            except (ValueError, AttributeError) as e:
                logger.error(f"Error converting GPS coordinates: {e}")
                return None
        else:
            logger.warning(f"Missing GPS EXIF tags in the image metadata for {picture_url.split('/')[-1]}.")
            return None
    else:
        if response:
            logger.error(f"Failed to fetch image. HTTP Status Code: {response.status_code}")
        return None

def _extract_identifier(filename):
    last_part = os.path.basename(filename).split('_')[-1].lower()
    return re.sub(r'(wide|med|tele|zoom)?\.jpg$', '', last_part)


def process_mission(folder, files, base_url, existing_missions, existing_gdf, rows, max_workers):
    jpg_files = [f for f in files if f.lower().endswith('.jpg')]
    closeup_files = [f for f in jpg_files if 'tele' in f.lower() or 'zoom' in f.lower()]
    med_files = [f for f in jpg_files if 'med' in f.lower()]
    wide_files = [f for f in jpg_files if 'tele' not in f.lower() and 'zoom' not in f.lower() and 'med' not in f.lower()]

    wide_lookup = {_extract_identifier(f): f for f in wide_files}
    med_lookup = {_extract_identifier(f): f for f in med_files}

    if len(wide_files) != len(closeup_files):
        logger.warning(f"Mission '{folder}': Number of wide files ({len(wide_files)}) does not match number of close-up files ({len(closeup_files)})")
    if med_files and len(med_files) != len(closeup_files):
        logger.warning(f"Mission '{folder}': Number of med files ({len(med_files)}) does not match number of close-up files ({len(closeup_files)})")

    rows_before = len(rows)
    n_closeup = len(closeup_files)

    if folder in existing_missions:
        existing_points = set()
        if existing_gdf is not None:
            existing_points = set(zip(
                existing_gdf[existing_gdf['mission_id'] == folder]['wide_url'],
                existing_gdf[existing_gdf['mission_id'] == folder]['point_id']
            ))
        closeup_files_to_add = []
        for closeup_file in closeup_files:
            identifier = _extract_identifier(closeup_file)
            wide_file = wide_lookup.get(identifier)
            wide_url = f"{base_url}/{folder}/{wide_file}" if wide_file else None
            if wide_url and (wide_url, identifier) not in existing_points:
                closeup_files_to_add.append(closeup_file)
        if not closeup_files_to_add:
            logger.info(f"All points for mission '{folder}' are already present. Skipping.")
            return
        logger.info(f"Adding {len(closeup_files_to_add)} missing points for mission '{folder}'.")
        closeup_files = closeup_files_to_add

    args_list = [(closeup_file, wide_lookup, med_lookup, folder, base_url) for closeup_file in closeup_files]
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        results = list(executor.map(process_closeup_file, args_list))
    for result in results:
        if result:
            rows.append(result)

    rows_added = len(rows) - rows_before
    logger.info(f"Finished mission '{folder}': {rows_added} points added, {len(wide_files)} wide images, {n_closeup} close-up images.")


def process_closeup_file(args):
    session = setup_session()

    closeup_file, wide_lookup, med_lookup, folder, base_url = args
    identifier = _extract_identifier(closeup_file)

    wide_file = wide_lookup.get(identifier)
    if not wide_file:
        logger.warning(f"Could not find matching wide photo for {closeup_file} with identifier {identifier}")
        return None

    med_file = med_lookup.get(identifier)

    wide_url = f"{base_url}/{folder}/{wide_file}"
    med_url = f"{base_url}/{folder}/{med_file}" if med_file else None
    tele_url = f"{base_url}/{folder}/{closeup_file}"
    coords = get_coordinates_from_image_url(wide_url, session)
    if coords:
        return {
            'geometry': Point(coords[1], coords[0]),
            'mission_id': folder,
            'point_id': identifier,
            'wide_url': wide_url,
            'med_url': med_url,
            'tele_url': tele_url
        }
    return None

def main(output_dir, points_layer, project_qualifier, max_workers=8):
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

    # Loop 1: legacy per-bucket missions (one S3 bucket per mission_id, zoom convention)
    # base URL is just the endpoint, files are at {ALLIANCECAN_URL}/{bucket}/{file}
    legacy_base_url = ALLIANCECAN_URL
    all_buckets = [b['Name'] for b in s3_client.list_buckets().get('Buckets', [])]
    legacy_folders = [b for b in all_buckets if project_qualifier.lower() in b.lower() and 'wpt' in b.lower()]
    logger.info(f"Found {len(legacy_folders)} legacy bucket(s) matching '{project_qualifier}'.")

    for folder in legacy_folders:
        paginator = s3_client.get_paginator('list_objects_v2')
        pages = paginator.paginate(Bucket=folder)
        files = [obj['Key'] for page in pages for obj in page.get('Contents', [])]
        process_mission(folder, files, legacy_base_url, existing_missions, existing_gdf, rows, max_workers)

    # Loop 2: new single-bucket missions (prefixes inside BUCKET_WPT, tele/zoom convention auto-detected)
    base_url = f"{ALLIANCECAN_URL}/{BUCKET_WPT}"
    prefix_paginator = s3_client.get_paginator('list_objects_v2')
    prefix_pages = prefix_paginator.paginate(Bucket=BUCKET_WPT, Delimiter='/')
    all_prefixes = [cp['Prefix'].rstrip('/') for page in prefix_pages for cp in page.get('CommonPrefixes', [])]
    folders = [p for p in all_prefixes if project_qualifier.lower() in p.lower() and 'wpt' in p.lower()]
    logger.info(f"Found {len(folders)} folder(s) in '{BUCKET_WPT}' matching '{project_qualifier}'.")

    for folder in folders:
        paginator = s3_client.get_paginator('list_objects_v2')
        pages = paginator.paginate(Bucket=BUCKET_WPT, Prefix=f"{folder}/")
        files = []
        for page in pages:
            for obj in page.get('Contents', []):
                rel = obj['Key'][len(folder) + 1:]
                if rel:
                    files.append(rel)
        process_mission(folder, files, base_url, existing_missions, existing_gdf, rows, max_workers)

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

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Process Arbutus BCI image folders in parallel.")
    parser.add_argument("--output_dir", required=True, help="Output directory for logs and points layer.")
    parser.add_argument("--project_qualifier", required=True, help="Project qualifier string.")
    parser.add_argument("--max_workers", type=int, default=8, help="Number of parallel workers.")
    parser.add_argument("--points_layer", required=False, help="Points layer filename to use or create. Defaults to '<project_qualifier>_wpt.gpkg'.")
    args = parser.parse_args()

    # Set default points_layer if not provided
    points_layer = args.points_layer if args.points_layer else f"{args.project_qualifier}_wpt.gpkg"

    main(
        output_dir=args.output_dir,
        points_layer=points_layer,
        project_qualifier=args.project_qualifier,
        max_workers=args.max_workers
    )
