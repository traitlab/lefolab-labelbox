# On Windows, run these commands first to activate the ArcGIS Pro Python environment:
# "C:/Program Files/ArcGIS/Pro/bin/Python/Scripts/activate.bat"
# conda activate arcgispro-py3

import argparse
import os
import logging
import sys

from arcgis.gis import GIS
from arcgis.features import FeatureLayerCollection
from dotenv import load_dotenv
from pathlib import Path

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)

stdout_handler = logging.StreamHandler(sys.stdout)
stdout_handler.setLevel(logging.INFO)
stdout_handler.addFilter(lambda record: record.levelno == logging.INFO)
stdout_handler.setFormatter(logging.Formatter('[%(asctime)s] %(message)s', datefmt='%Y-%m-%d %H:%M:%S'))

stderr_handler = logging.StreamHandler(sys.stderr)
stderr_handler.setLevel(logging.WARNING)
stderr_handler.setFormatter(logging.Formatter('[%(asctime)s] %(message)s', datefmt='%Y-%m-%d %H:%M:%S'))

logger.handlers = []
logger.addHandler(stdout_handler)
logger.addHandler(stderr_handler)

def update_layer(project_name, shp_path):
    project_root = Path(__file__).parent.parent.parent
    logger.info(f"Loading environment variables from {project_root / '.env'}")
    load_dotenv(dotenv_path=project_root / '.env')

    ArcGIS_username = os.getenv("AGOL_USER")
    ArcGIS_password = os.getenv("AGOL_PASSWORD")
    if not ArcGIS_username:
        logger.error('AGOL_USER environment variable is not set')
        sys.exit(1)
    if not ArcGIS_password:
        logger.error('AGOL_PASSWORD environment variable is not set')
        sys.exit(1)

    proxy_http = os.getenv("PROXY_HTTP")
    proxy_https = os.getenv("PROXY_HTTPS")
    if not proxy_http or not proxy_https:
        logger.warning('Proxy settings not set. Proceeding without proxy.')
        proxy = None
    else:
        proxy = {
            'http': proxy_http,
            'https': proxy_https,
        }

    env_var = f"{project_name.upper()}_ITEM_ID"
    item_id = os.getenv(env_var)
    if not item_id:
        logger.error(f'Environment variable {env_var} is not set')
        sys.exit(1)

    update_shp = Path(shp_path) / f"{project_name}_wpt_3857.zip"
    if not update_shp.exists():
        logger.error(f"Shapefile not found: {update_shp}")
        sys.exit(1)

    logger.info("Connecting to ArcGIS Online...")
    gis = GIS("https://lefo.maps.arcgis.com/", ArcGIS_username, ArcGIS_password, proxy=proxy)
    logger.info(f"Logged to {gis.url} as {gis.properties.user.username}")

    logger.info(f"Getting content item: {item_id}")
    layer = gis.content.get(item_id)
    if not layer:
        logger.error(f"Content item {item_id} not found.")
        sys.exit(1)
    flc = FeatureLayerCollection.fromitem(layer)

    logger.info(f"Overwriting layer with file: {update_shp}")
    try:
        flc.manager.overwrite(update_shp)
        logger.info("Layer updated successfully.")
    except Exception as e:
        logger.error(f"Failed to update layer: {e}")
        sys.exit(1)

def main():
    parser = argparse.ArgumentParser(description="Update ArcGIS Online layer with zipped shapefile.")
    parser.add_argument('--project_name', required=True, help='Project name')
    parser.add_argument('--shp_path', required=True, help='Directory containing zipped shapefile')
    args = parser.parse_args()

    update_layer(args.project_name, args.shp_path)

if __name__ == "__main__":
    main()
