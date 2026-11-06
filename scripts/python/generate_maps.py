import argparse
import boto3
import branca.colormap as bcm
import folium
import logging
import matplotlib.colors as colors
import numpy as np
import os
import rasterio
import sys
import time

from botocore import UNSIGNED
from botocore.client import Config
from dotenv import load_dotenv
from folium import Element
from folium import IFrame
from matplotlib import colormaps
from pathlib import Path
from pyproj import Transformer

from _common import get_coordinates_from_image_url

# Longest side (px) of the overlay embedded in each map
OVERLAY_MAX_SIZE = 2048

LEGEND_CSS = """
<style>
.legend.leaflet-control text.caption {
    font-size: 16px;
    font-weight: bold;
    fill: white !important;
}
.legend.leaflet-control .tick text {
    font-size: 14px;
    fill: white;
    font-weight: bold;
}
</style>
"""

def find_dsm(conrad_path, site_name, mapping_mission=None):
    """
    Find the DSM of the given mapping mission, or of the most recent mapping mission of the site.

    Args:
        conrad_path (str): Root of the Conrad data (contains metashape/YYYY/<mapping_mission>).
        site_name (str): Site keyword (second segment of the mission_id).
        mapping_mission (str, optional): Explicit mapping mission name.

    Returns:
        str or None: Path to the DSM GeoTIFF (COG preferred), or None if not found.
    """
    pattern = mapping_mission or f"*_{site_name}_*"
    folders = sorted(Path(conrad_path, 'metashape').glob(f"*/{pattern}"), key=lambda p: p.name, reverse=True)
    for folder in folders:
        for suffix in ('_dsm.cog.tif', '_dsm.tif'):
            dsm = folder / f"{folder.name}{suffix}"
            if dsm.exists():
                return str(dsm)
    return None

def load_overlay(raster_path, caption):
    """
    Render a downsampled elevation raster as a turbo-coloured overlay with its legend.

    Args:
        raster_path (str): Path to the elevation GeoTIFF.
        caption (str): Legend caption.

    Returns:
        dict: path, image (RGBA uint8), bounds ([[south, west], [north, east]]) and colormap.
    """
    with rasterio.open(raster_path) as src:
        scale = max(1, max(src.width, src.height) / OVERLAY_MAX_SIZE)
        data = src.read(1, masked=True, out_shape=(int(src.height / scale), int(src.width / scale)))
        transformer = Transformer.from_crs(src.crs, "EPSG:4326", always_xy=True)
        west, south = transformer.transform(src.bounds.left, src.bounds.bottom)
        east, north = transformer.transform(src.bounds.right, src.bounds.top)

    data = np.ma.masked_invalid(data)
    vmin, vmax = float(data.min()), float(data.max())
    cmap = colormaps.get_cmap('turbo')
    norm = colors.Normalize(vmin=vmin, vmax=vmax)

    image = cmap(norm(data.filled(vmin)))
    image[..., 3] = ~np.ma.getmaskarray(data)

    colormap = bcm.StepColormap(
        colors=[cmap(norm(v)) for v in np.linspace(vmin, vmax, 10)],
        vmin=vmin, vmax=vmax,
        caption=caption,
        text_color='white'
    )

    return {
        'path': raster_path,
        'image': (image * 255).astype(np.uint8),
        'bounds': [[south, west], [north, east]],
        'colormap': colormap,
    }

def sample_raster(raster_path, lat, lon):
    """
    Get the raster value at a geographic location.

    Args:
        raster_path (str): Path to the raster file.
        lat (float): Latitude in decimal degrees.
        lon (float): Longitude in decimal degrees.

    Returns:
        float or None: Raster value, or None if outside the raster or nodata.
    """
    with rasterio.open(raster_path) as src:
        x, y = Transformer.from_crs("EPSG:4326", src.crs, always_xy=True).transform(lon, lat)
        value = next(src.sample([(x, y)], masked=True))[0]
    if np.ma.is_masked(value) or np.isnan(value):
        return None
    return float(value)

def create_map(lat, lon, output_path, dsm_overlay, dtm_overlay=None):
    """
    Create an interactive map with a marker and a DTM or DSM overlay.
    The DTM is used when available at the point (with tree height), otherwise the DSM.

    Args:
        lat (float): Latitude of the marker.
        lon (float): Longitude of the marker.
        output_path (str): Path to save the generated HTML file.
        dsm_overlay (dict): DSM overlay from load_overlay.
        dtm_overlay (dict, optional): DTM overlay from load_overlay.
    """
    logger = logging.getLogger('MapGenerator')

    m = folium.Map(
        location=[lat, lon],
        zoom_start=18,
        max_zoom=20,
        tiles="https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}",
        attr="Esri"
    )

    popup_content = [
        f"<b>Lat:</b> {lat:.6f}°",
        f"<b>Lon:</b> {lon:.6f}°"
    ]

    overlay = dsm_overlay
    dtm_value = sample_raster(dtm_overlay['path'], lat, lon) if dtm_overlay else None
    if dtm_value is not None:
        overlay = dtm_overlay
        dsm_value = sample_raster(dsm_overlay['path'], lat, lon)
        if dsm_value is not None:
            popup_content.append(f"<b>Tree height:</b> {dsm_value - dtm_value:.2f} m")
        else:
            logger.warning(f"No DSM value at ({lat:.8f}, {lon:.8f}), tree height not computed.")

    html = f"""
    <div style="width: 150px; height: 60px; overflow: hidden;">
        {'<br>'.join(popup_content)}
    </div>
    """

    iframe = IFrame(html, width=180, height=80)
    popup = folium.Popup(iframe)
    folium.Marker([lat, lon], popup=popup).add_to(m)

    folium.raster_layers.ImageOverlay(
        image=overlay['image'],
        bounds=overlay['bounds'],
        opacity=0.7,
        interactive=False,
    ).add_to(m)
    m.add_child(overlay['colormap'])
    m.get_root().html.add_child(Element(LEGEND_CSS))

    m.save(output_path)

def setup_logging(mission_id, output_dir):
    """Configure logging to separate files and streams by level."""
    log_dir = os.path.join(output_dir, mission_id, 'labelbox')
    os.makedirs(log_dir, exist_ok=True)

    info_log_file = os.path.join(log_dir, f'{mission_id}_maps.log')
    error_log_file = os.path.join(log_dir, f'{mission_id}_maps_error.log')

    logger = logging.getLogger('MapGenerator')
    logger.setLevel(logging.INFO)
    logger.handlers = []

    formatter = logging.Formatter('[%(asctime)s] %(message)s', datefmt='%Y-%m-%d %H:%M:%S')

    info_stream_handler = logging.StreamHandler(sys.stdout)
    info_stream_handler.setLevel(logging.INFO)
    info_stream_handler.addFilter(lambda record: record.levelno == logging.INFO)
    info_stream_handler.setFormatter(formatter)

    info_file_handler = logging.FileHandler(info_log_file)
    info_file_handler.setLevel(logging.INFO)
    info_file_handler.addFilter(lambda record: record.levelno == logging.INFO)
    info_file_handler.setFormatter(formatter)

    error_stream_handler = logging.StreamHandler(sys.stderr)
    error_stream_handler.setLevel(logging.WARNING)
    error_stream_handler.setFormatter(formatter)

    error_file_handler = logging.FileHandler(error_log_file)
    error_file_handler.setLevel(logging.WARNING)
    error_file_handler.setFormatter(formatter)

    logger.addHandler(info_stream_handler)
    logger.addHandler(info_file_handler)
    logger.addHandler(error_stream_handler)
    logger.addHandler(error_file_handler)

    return logger

def main(mission_id, project_name, bucket=None, mapping_mission=None):
    """Main function to process a mission"""
    start_time = time.time()

    project_root = Path(__file__).parent.parent.parent
    project_dir = project_root / 'projects' / project_name
    output_dir = str(project_dir / 'output_maps')

    logger = setup_logging(mission_id, output_dir)
    logger.info(f"Processing mission: {mission_id}")

    load_dotenv(dotenv_path=project_root / '.env')

    ALLIANCECAN_URL = os.getenv("ALLIANCECAN_URL")
    CONRAD_PATH = os.getenv("CONRAD_PATH")
    AWS_ACCESS_KEY_ID = os.getenv("AWS_ACCESS_KEY_ID")
    AWS_SECRET_ACCESS_KEY = os.getenv("AWS_SECRET_ACCESS_KEY")

    if not ALLIANCECAN_URL:
        logger.error("ALLIANCECAN_URL environment variable is not set")
        raise ValueError("ALLIANCECAN_URL environment variable is not set")
    if not CONRAD_PATH:
        logger.error("CONRAD_PATH environment variable is not set")
        raise ValueError("CONRAD_PATH environment variable is not set")
    if not AWS_ACCESS_KEY_ID or not AWS_SECRET_ACCESS_KEY:
        logger.warning("AWS_ACCESS_KEY_ID and/or AWS_SECRET_ACCESS_KEY environment variables are not set. Assuming public bucket access.")

    # Drone site (second segment of mission_id)
    site_name = mission_id.split('_')[1]

    # DTM - site-level, else project-level
    dtm_tifs = [project_dir / f'{site_name}_dtm.tif', project_dir / f'{project_name}_dtm.tif']
    dtm_path = next((str(p) for p in dtm_tifs if p.exists()), None)

    # DSM - from the site's mapping mission
    dsm_path = find_dsm(CONRAD_PATH, site_name, mapping_mission)

    if not dsm_path:
        logger.error(f"No DSM found for site '{site_name}' in {CONRAD_PATH}/metashape. Specify a mapping mission manually.")
        raise ValueError(f"No DSM available for mission {mission_id}")
    logger.info(f"Using DSM: {dsm_path}")
    logger.info(f"Using DTM: {dtm_path}" if dtm_path else f"No DTM found for site '{site_name}' or project '{project_name}'.")

    dsm_overlay = load_overlay(dsm_path, 'Digital surface model - Modelo digital de superficie (m)')
    dtm_overlay = load_overlay(dtm_path, 'Digital terrain model - Modelo digital de terreno (m)') if dtm_path else None

    # Per-project bucket (no underscores allowed): <bucket>/drone_missions/<yyyy>/<mission>/...
    project_bucket = bucket or project_name.replace("_", "-")
    prefix = f"drone_missions/{mission_id[:4]}/{mission_id}"

    # The S3 API lives at the bare host; ALLIANCECAN_URL carries the Swift path for asset URLs
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

    try:
        paginator = s3_client.get_paginator('list_objects_v2')
        pages = paginator.paginate(Bucket=project_bucket, Prefix=prefix)

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

    folder_url = f"{ALLIANCECAN_URL}/{project_bucket}"

    logger.info(f"{len(file_keys)} pictures found for this mission : {mission_id}")

    closeup_files = [key for key in file_keys if "tele" in key]
    if closeup_files:
        logger.info(f"{len(closeup_files)} close-up pictures (tele) found for this mission : {mission_id}")
    else:
        closeup_files = [key for key in file_keys if "zoom" in key]
        if closeup_files:
            logger.info("Using legacy naming convention (zoom).")
            logger.info(f"{len(closeup_files)} close-up pictures (zoom) found for this mission : {mission_id}")
        else:
            logger.warning(f"No close-up pictures found for this mission : {mission_id}")

    output_folder = f"{output_dir}/{mission_id}/labelbox/attachments"
    os.makedirs(output_folder, exist_ok=True)

    maps_created = 0
    errors_occurred = 0

    for closeup_file in closeup_files:
        if "tele" in closeup_file:
            polygon_id = closeup_file.split('_')[-1].lower().replace('tele.jpg', '')
            wide_file_end = f"_{polygon_id}wide.JPG"
            exclusion_keyword = "tele"
        else:
            polygon_id = closeup_file.split('_')[-1].lower().replace('zoom.jpg', '')
            wide_file_end = f"_{polygon_id}.JPG"
            exclusion_keyword = "zoom"

        matching_wide_files = [key for key in file_keys if wide_file_end in key and exclusion_keyword not in key]

        if len(matching_wide_files) > 1:
            logger.warning(f"Warning: Multiple wide pictures found for {closeup_file}: {matching_wide_files}. Using the first match.")

        if not matching_wide_files:
            logger.warning(f"No wide file found for {closeup_file}")
            errors_occurred += 1
            continue

        wide_coordinates = get_coordinates_from_image_url(f'{folder_url}/{matching_wide_files[0]}')
        if not wide_coordinates:
            logger.warning(f"No coordinates found for {closeup_file}")
            errors_occurred += 1
            continue

        lat, lon = wide_coordinates
        filename = os.path.splitext(os.path.basename(closeup_file))[0]
        output_file = f"{output_folder}/{filename}.html"

        try:
            create_map(lat, lon, output_file, dsm_overlay, dtm_overlay=dtm_overlay)
            maps_created += 1
        except Exception as e:
            logger.error(f"Error creating map for {closeup_file}: {str(e)}")
            errors_occurred += 1

    logger.info(f"Mission {mission_id}: {maps_created} maps created, {errors_occurred} errors in {time.time() - start_time:.1f} s")

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description='Process drone close-up pictures mission data and create maps.')
    parser.add_argument('--mission_id', required=True, help='ID of the mission to process')
    parser.add_argument('--project_name', required=True, help='Project name (folder under projects/)')
    parser.add_argument('--bucket', help='Arbutus bucket of the mission (default: project name with hyphens)')
    parser.add_argument('--mapping_mission', help='Mapping mission to take the DSM from (default: most recent for the site)')
    args = parser.parse_args()

    try:
        main(args.mission_id, args.project_name, args.bucket, args.mapping_mission)
    except Exception as e:
        logging.getLogger('MapGenerator').error(f"Fatal error: {str(e)}")
        sys.exit(1)
