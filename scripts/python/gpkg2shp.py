import geopandas as gpd
import logging
import sys
import zipfile
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

SHP_EXTS = [".shp", ".shx", ".dbf", ".prj", ".cpg"]

def delete_old_files(base_path: Path):
    for ext in SHP_EXTS + [".zip"]:
        p = base_path.with_suffix(ext)
        if p.exists():
            try:
                p.unlink()
                logger.info(f"Deleted old file: {p}")
            except Exception as e:
                logger.error(f"Failed to delete {p}: {e}")

def convert_and_zip(input_gpkg: Path, output_dir: Path):
    if not output_dir.is_dir():
        logger.error(f"Output directory does not exist: {output_dir}")
        sys.exit(1)

    logger.info(f"Reading GeoPackage: {input_gpkg}")
    try:
        gdf = gpd.read_file(input_gpkg)
    except Exception as e:
        logger.error(f"Failed to read GeoPackage: {e}")
        sys.exit(1)

    logger.info("Reprojecting to EPSG:3857")
    try:
        gdf = gdf.to_crs(epsg=3857)
    except Exception as e:
        logger.error(f"Failed to reproject: {e}")
        sys.exit(1)

    shp_path = output_dir / (input_gpkg.stem + "_3857.shp")
    base_path = shp_path.with_suffix("")

    delete_old_files(base_path)

    logger.info(f"Exporting to shapefile: {shp_path}")
    try:
        gdf.to_file(shp_path)
    except Exception as e:
        logger.error(f"Failed to export shapefile: {e}")
        sys.exit(1)

    files = [base_path.with_suffix(ext) for ext in SHP_EXTS if base_path.with_suffix(ext).exists()]
    zip_path = base_path.with_suffix(".zip")
    logger.info(f"Zipping shapefile components to: {zip_path}")
    try:
        with zipfile.ZipFile(zip_path, "w") as zf:
            for f in files:
                zf.write(f, arcname=f.name)
        logger.info(f"Zipped shapefile: {zip_path}")
    except Exception as e:
        logger.error(f"Failed to zip shapefile components: {e}")
        sys.exit(1)

    for f in files:
        try:
            f.unlink()
            logger.info(f"Deleted shapefile component: {f}")
        except Exception as e:
            logger.error(f"Failed to delete {f}: {e}")

if __name__ == "__main__":
    if len(sys.argv) != 3:
        logger.error("Usage: python gpkg2shp.py input.gpkg output_dir")
        sys.exit(1)
    convert_and_zip(Path(sys.argv[1]), Path(sys.argv[2]))
