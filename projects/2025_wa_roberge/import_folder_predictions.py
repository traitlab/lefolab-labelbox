"""
Import folder-name predictions into a Labelbox Model Run.

Walks a local folder of species/genus/family subfolders (ignoring 'not_used'),
maps each tele image to the folder name (stripped of the trailing GBIF numeric ID),
then uploads a full-image bounding-box prediction with the taxon name to Labelbox.

The folder name format is expected to be "<Taxon name>-<GBIF numeric ID>", e.g.:
  "Banksia attenuata-5636437"  →  taxon label "Banksia attenuata"
  "Banksia-8399031"            →  taxon label "Banksia"

The taxon label must match an option in the ontology (Taxon radio class).
The script reads the full-image resolution from the first available tele image using
Pillow (EXIF/header), defaulting to 4000×3000 if reading fails.

Usage:
  python projects/2025_wa_roberge/import_folder_predictions.py \\
      --folder "G:\\temp\\H2026_close-up" \\
      --dataset "2025_wa_roberge" \\
      --ontology "2025_wa_roberge_plants" \\
      --model "Folder labels - 2025_wa_roberge" \\
      --run "H2026_close-up"

Optional:
  --test            Process only first 5 predictions
  --confidence 1.0  Confidence score for predictions (default: 1.0)
"""

import argparse
import logging
import os
import sys
import time
from pathlib import Path

import labelbox as lb
import labelbox.types as lb_types
from dotenv import load_dotenv

try:
    from PIL import Image as PILImage
    _PIL_AVAILABLE = True
except ImportError:
    _PIL_AVAILABLE = False

# ── Logging ────────────────────────────────────────────────────────────────────

logger = logging.getLogger()
logger.setLevel(logging.INFO)

stdout_handler = logging.StreamHandler(sys.stdout)
stdout_handler.setLevel(logging.INFO)
stdout_handler.addFilter(lambda record: record.levelno == logging.INFO)
stdout_handler.setFormatter(logging.Formatter("[%(asctime)s] %(message)s", datefmt="%Y-%m-%d %H:%M:%S"))

stderr_handler = logging.StreamHandler(sys.stderr)
stderr_handler.setLevel(logging.WARNING)
stderr_handler.setFormatter(logging.Formatter("[%(asctime)s] %(message)s", datefmt="%Y-%m-%d %H:%M:%S"))

logger.handlers = []
logger.addHandler(stdout_handler)
logger.addHandler(stderr_handler)

# ── Constants ──────────────────────────────────────────────────────────────────

DEFAULT_IMAGE_WIDTH  = 4000
DEFAULT_IMAGE_HEIGHT = 3000
BATCH_SIZE = 100

# Ontology field names (must match the project's ontology exactly)
BBOX_TOOL_NAME   = "Plants"
TAXON_CLASS_NAME = "Taxon"


# ── Helpers ────────────────────────────────────────────────────────────────────

def strip_gbif_suffix(folder_name: str) -> str:
    """'Banksia attenuata-5636437' → 'Banksia attenuata'"""
    parts = folder_name.rsplit("-", 1)
    if len(parts) == 2 and parts[1].isdigit():
        return parts[0].strip()
    return folder_name.strip()


def read_image_size(path: Path) -> tuple[int, int]:
    """Return (width, height) from image file, or default on failure."""
    if not _PIL_AVAILABLE:
        logger.warning("Pillow not installed — using default %dx%d", DEFAULT_IMAGE_WIDTH, DEFAULT_IMAGE_HEIGHT)
        return DEFAULT_IMAGE_WIDTH, DEFAULT_IMAGE_HEIGHT
    try:
        with PILImage.open(path) as img:
            return img.size  # (width, height)
    except Exception as e:
        logger.warning("Could not read image size from %s: %s. Using default.", path.name, e)
        return DEFAULT_IMAGE_WIDTH, DEFAULT_IMAGE_HEIGHT


def collect_predictions(folder: Path) -> list[dict]:
    """
    Walk subfolders (skip 'not_used') and collect {global_key, taxon_name} for
    every tele .JPG file found.
    """
    predictions = []
    for subfolder in sorted(folder.iterdir()):
        if not subfolder.is_dir():
            continue
        if subfolder.name.lower() == "not_used":
            continue

        taxon_name = strip_gbif_suffix(subfolder.name)

        for img_path in subfolder.iterdir():
            if not img_path.is_file():
                continue
            if "tele" not in img_path.name.lower():
                continue
            if not img_path.suffix.upper() == ".JPG":
                continue
            predictions.append({
                "global_key": img_path.name,
                "taxon_name": taxon_name,
                "path": img_path,
            })

    return predictions


def get_or_create_model_run(client: lb.Client, ontology_name: str,
                             model_name: str, run_name: str) -> lb.ModelRun:
    ontology = next((o for o in client.get_ontologies(ontology_name) if o.name == ontology_name), None)
    if ontology is None:
        logger.error("Ontology '%s' not found in Labelbox.", ontology_name)
        sys.exit(1)
    logger.info("Ontology: '%s' (%s)", ontology.name, ontology.uid)

    model = next((m for m in client.get_models() if m.name == model_name), None)
    if model is None:
        logger.info("Creating model '%s'...", model_name)
        model = client.create_model(
            name=model_name,
            ontology_id=ontology.uid,
        )
    else:
        logger.info("Found model '%s' (%s)", model_name, model.uid)

    run = next((r for r in model.model_runs() if r.name == run_name), None)
    if run is None:
        logger.info("Creating model run '%s'...", run_name)
        run = model.create_model_run(run_name)
    else:
        logger.info("Found model run '%s' (%s)", run_name, run.uid)

    return run


def build_label(global_key: str, taxon_name: str, img_w: int, img_h: int,
                confidence: float) -> lb_types.Label:
    bbox = lb_types.ObjectAnnotation(
        name=BBOX_TOOL_NAME,
        confidence=confidence,
        value=lb_types.Rectangle(
            start=lb_types.Point(x=0, y=0),
            end=lb_types.Point(x=img_w, y=img_h),
        ),
        classifications=[
            lb_types.ClassificationAnnotation(
                name=TAXON_CLASS_NAME,
                value=lb_types.Radio(
                    answer=lb_types.ClassificationAnswer(
                        name=taxon_name,
                        confidence=confidence,
                    )
                ),
            ),
        ],
    )
    return lb_types.Label(
        data={"global_key": global_key},
        annotations=[bbox],
    )


# ── Main ───────────────────────────────────────────────────────────────────────

def main():
    project_root = Path(__file__).parent.parent.parent
    load_dotenv(dotenv_path=project_root / ".env")

    parser = argparse.ArgumentParser(
        description="Import folder-name predictions into a Labelbox project as a Model Run."
    )
    parser.add_argument("--folder", required=True,
                        help="Path to the folder containing species/genus/family subfolders")
    parser.add_argument("--dataset", required=True,
                        help="Labelbox dataset name (e.g. '2025_wa_roberge') — used to resolve full global keys")
    parser.add_argument("--ontology", required=True,
                        help="Labelbox ontology name (e.g. '2025_wa_roberge_plants')")
    parser.add_argument("--model", required=True,
                        help="Model name in Labelbox (created if it does not exist)")
    parser.add_argument("--run", required=True,
                        help="Model run name (created if it does not exist)")
    parser.add_argument("--confidence", type=float, default=1.0,
                        help="Prediction confidence score, 0.0–1.0 (default: 1.0)")
    parser.add_argument("--test", action="store_true",
                        help="Process only the first 5 predictions")
    args = parser.parse_args()

    folder = Path(args.folder)
    if not folder.is_dir():
        logger.error("Folder not found: %s", folder)
        sys.exit(1)

    api_key = os.environ.get("LABELBOX_API_KEY")
    if not api_key:
        logger.error("LABELBOX_API_KEY not set in .env")
        sys.exit(1)

    # ── Collect predictions from folders ──────────────────────────────────────
    logger.info("Scanning folder: %s", folder)
    raw = collect_predictions(folder)
    logger.info("Found %d tele images across %d subfolders",
                len(raw),
                len({p["global_key"][:0] or p["taxon_name"] for p in raw}))

    if not raw:
        logger.error("No tele images found. Exiting.")
        sys.exit(1)

    if args.test:
        raw = raw[:5]
        logger.info("TEST MODE: processing %d predictions only", len(raw))

    # ── Read image size from first available file ─────────────────────────────
    img_w, img_h = read_image_size(raw[0]["path"])
    logger.info("Image size: %dx%d", img_w, img_h)

    # ── Connect to Labelbox ───────────────────────────────────────────────────
    logger.info("Connecting to Labelbox...")
    client = lb.Client(api_key=api_key, enable_experimental=True)

    # ── Build filename → full global key lookup from dataset ──────────────────
    logger.info("Loading global keys from dataset '%s'...", args.dataset)
    dataset = next((d for d in client.get_datasets() if d.name == args.dataset), None)
    if dataset is None:
        logger.error("Dataset '%s' not found in Labelbox.", args.dataset)
        sys.exit(1)
    # global keys are like "mission/DJI_....JPG" — index by filename only
    filename_to_gk = {
        row.global_key.split("/")[-1]: row.global_key
        for row in dataset.data_rows()
    }
    logger.info("Loaded %d global keys from dataset.", len(filename_to_gk))

    # ── Build labels ──────────────────────────────────────────────────────────
    logger.info("Building prediction labels...")
    labels = []
    global_keys = []
    skipped = 0

    for entry in raw:
        full_gk = filename_to_gk.get(entry["global_key"])
        if full_gk is None:
            logger.warning("No dataset match for '%s' — skipping.", entry["global_key"])
            skipped += 1
            continue
        label = build_label(
            global_key=full_gk,
            taxon_name=entry["taxon_name"],
            img_w=img_w,
            img_h=img_h,
            confidence=args.confidence,
        )
        labels.append(label)
        global_keys.append(full_gk)

    logger.info("Labels built: %d, skipped (not in dataset): %d", len(labels), skipped)

    # ── Set up model run ──────────────────────────────────────────────────────
    logger.info("Setting up model run...")
    model_run = get_or_create_model_run(client, args.ontology, args.model, args.run)
    model_run.update_config({"iou_threshold": 0.0})
    logger.info("Model run ready: %s", model_run.uid)

    # ── Register data rows ────────────────────────────────────────────────────
    logger.info("Registering %d data rows with model run...", len(global_keys))
    model_run.upsert_data_rows(global_keys=global_keys)

    # ── Upload predictions in batches ─────────────────────────────────────────
    logger.info("Uploading %d predictions in batches of %d...", len(labels), BATCH_SIZE)
    run_ts = int(time.time())
    total_ok = total_err = 0

    for i in range(0, len(labels), BATCH_SIZE):
        batch = labels[i:i + BATCH_SIZE]
        batch_num = i // BATCH_SIZE + 1
        job = model_run.add_predictions(
            name=f"folder_pred_{run_ts}_b{batch_num}",
            predictions=batch,
        )
        job.wait_till_done()
        errs = job.errors
        ok = len(batch) - len(errs)
        total_ok += ok
        total_err += len(errs)
        logger.info("Batch %d: %d ok, %d errors (%d/%d total)",
                    batch_num, ok, len(errs), total_ok, len(labels))
        if errs:
            for e in errs[:3]:
                logger.warning("  ERROR: %s", e)

    # ── Summary ───────────────────────────────────────────────────────────────
    logger.info("=" * 55)
    logger.info("  Ontology:     %s", args.ontology)
    logger.info("  Model run:    %s (%s)", args.run, model_run.uid)
    logger.info("  Uploaded OK:  %d / %d", total_ok, len(labels))
    logger.info("  Errors:       %d", total_err)
    logger.info("  Skipped:      %d (not found in dataset)", skipped)
    logger.info("=" * 55)

    if args.test:
        logger.info("TEST MODE done. Review in Labelbox, then run without --test.")


if __name__ == "__main__":
    main()
