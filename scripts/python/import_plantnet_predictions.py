"""Import Pl@ntNet predictions as MAL prelabels for a mission already sent to a Labelbox project.

Settings come from projects/<project>/config_plantnet.py. Pl@ntNet sees the
center CROP_SIZE x CROP_SIZE crop, and the prelabel is a box over that crop
with the nested Taxon answer.

Usage:
  python scripts/python/import_plantnet_predictions.py --project 2024_qc --mission 20260806_sblextraouest1_wpt_m4e [--test]
"""

import argparse
import importlib.util
import io
import json
import os
import sys
import time
import uuid

import labelbox as lb
import requests
from PIL import Image

from _common import PROJECT_ROOT, get_client, resolve_project, setup_logging

logger = setup_logging()


def load_config(project):
    path = PROJECT_ROOT / "projects" / project / "config_plantnet.py"
    spec = importlib.util.spec_from_file_location("config_plantnet", path)
    cfg = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(cfg)
    return cfg


def mission_data_rows(client, dataset_name, mission):
    """Return [(global_key, row_data)] of the dataset rows whose mission metadata matches."""
    dataset = next((ds for ds in client.get_datasets() if ds.name == dataset_name), None)
    if dataset is None:
        logger.error(f"Dataset '{dataset_name}' not found.")
        sys.exit(1)

    rows = []

    def handle_export(output: lb.BufferedJsonConverterOutput):
        row = output.json
        if any(mf.get("schema_name") == "mission" and mf.get("value") == mission
               for mf in row.get("metadata_fields", [])):
            rows.append((row["data_row"]["global_key"], row["data_row"]["row_data"]))

    export_task = dataset.export(params={"metadata_fields": True})
    export_task.wait_till_done()
    if export_task.has_errors():
        logger.error("Export of dataset '%s' failed.", dataset_name)
        sys.exit(1)
    export_task.get_buffered_stream(stream_type=lb.StreamType.RESULT).start(stream_handler=handle_export)
    return sorted(rows)


def center_crop(image_bytes, size):
    """Return (jpeg bytes of the center crop, crop box (x0, y0, x1, y1) in original pixels)."""
    img = Image.open(io.BytesIO(image_bytes)).convert("RGB")
    w, h = img.size
    if w >= size and h >= size:
        left, top = (w - size) // 2, (h - size) // 2
        box = (left, top, left + size, top + size)
        img = img.crop(box)
    else:
        box = (0, 0, w, h)
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=90)
    return buf.getvalue(), box


def identify(cfg, global_key, row_data, api_key):
    """Call Pl@ntNet on the center crop; results are cached per global key."""
    cache_path = PROJECT_ROOT / cfg.CACHE_DIR / f"{global_key}.json"
    if cache_path.exists():
        return json.loads(cache_path.read_text(encoding="utf-8"))

    image = requests.get(row_data, timeout=60)
    image.raise_for_status()
    jpeg, box = center_crop(image.content, cfg.CROP_SIZE)

    for attempt in range(3):
        resp = requests.post(
            cfg.PLANTNET_URL,
            files=[("images", (global_key, jpeg, "image/jpeg"))],
            data={"organs": cfg.PLANTNET_ORGANS},
            params={"api-key": api_key, "nb-results": cfg.PLANTNET_NB_RESULTS,
                    "no-reject": "true", "include-related-images": "false", "lang": "en"},
            timeout=120,
        )
        if resp.status_code == 200:
            break
        if resp.status_code == 429:
            logger.error("Pl@ntNet quota exceeded. Re-run later, cached results are kept.")
            sys.exit(1)
        logger.warning(f"Pl@ntNet HTTP {resp.status_code} for {global_key}: {resp.text[:200]}")
        time.sleep(5 * (attempt + 1))
    else:
        return None
    time.sleep(cfg.PLANTNET_DELAY)

    result = {"crop_box": box, "response": resp.json()}
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    cache_path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    return result


def taxon_lookup(project, cfg):
    """Map GBIF id and scientific name to the Taxon option label of the project ontology."""
    tool = next(t for t in project.ontology().normalized["tools"] if t["name"] == cfg.BBOX_TOOL_NAME)
    taxon = next(c for c in tool["classifications"] if c["name"] == cfg.TAXON_CLASS_NAME)
    by_gbif, by_name = {}, {}
    for option in taxon["options"]:
        by_gbif[str(option["value"])] = option["label"]
        by_name[option["label"].split(cfg.LABEL_SEPARATOR, 1)[0].strip()] = option["label"]
    return by_gbif, by_name


def match_taxon(top, by_gbif, by_name):
    """Top Pl@ntNet result -> option label: GBIF id, then species, genus, family name."""
    species = top.get("species", {})
    gbif_id = str(top.get("gbif", {}).get("id", ""))
    if gbif_id in by_gbif:
        return by_gbif[gbif_id], "gbif"
    for level, name in (("species", species.get("scientificNameWithoutAuthor")),
                        ("genus", species.get("genus", {}).get("scientificNameWithoutAuthor")),
                        ("family", species.get("family", {}).get("scientificNameWithoutAuthor"))):
        if name in by_name:
            return by_name[name], level
    return None, None


def build_label(cfg, global_key, crop_box, taxon):
    x0, y0, x1, y1 = crop_box
    return {
        "uuid": str(uuid.uuid4()),
        "name": cfg.BBOX_TOOL_NAME,
        "dataRow": {"globalKey": global_key},
        "bbox": {"left": x0, "top": y0, "width": x1 - x0, "height": y1 - y0},
        "classifications": [{"name": cfg.TAXON_CLASS_NAME, "answer": {"name": taxon}}],
    }


def main():
    parser = argparse.ArgumentParser(description="Import Pl@ntNet MAL prelabels for a mission.")
    parser.add_argument("--project", required=True, help="Project/dataset name, with projects/<project>/config_plantnet.py.")
    parser.add_argument("--mission", required=True, help="Mission ID of the data rows.")
    parser.add_argument("--test", action="store_true", help="Only call Pl@ntNet on 3 pictures, upload nothing.")
    args = parser.parse_args()

    cfg = load_config(args.project)
    api_key = os.getenv("PLANTNET_API_KEY")
    if not api_key:
        logger.error("PLANTNET_API_KEY environment variable is not set")
        sys.exit(1)

    client = get_client()
    project = resolve_project(client, args.project)
    by_gbif, by_name = taxon_lookup(project, cfg)

    rows = mission_data_rows(client, args.project, args.mission)
    if not rows:
        logger.error(f"No data rows found for mission '{args.mission}' in dataset '{args.project}'.")
        sys.exit(1)
    logger.info(f"{len(rows)} data rows found for mission '{args.mission}'.")
    if args.test:
        rows = rows[:3]

    labels, counts = [], {}
    for i, (global_key, row_data) in enumerate(rows, 1):
        result = identify(cfg, global_key, row_data, api_key)
        results = (result or {}).get("response", {}).get("results", [])
        if not results:
            logger.warning(f"No Pl@ntNet result for {global_key}.")
            counts["no result"] = counts.get("no result", 0) + 1
            continue
        top = results[0]
        taxon, level = match_taxon(top, by_gbif, by_name)
        counts[level or "unmatched"] = counts.get(level or "unmatched", 0) + 1
        pn_name = top.get("species", {}).get("scientificNameWithoutAuthor")
        if args.test or taxon is None:
            logger.info(f"[{i}/{len(rows)}] {global_key}: {pn_name} ({top['score']:.2f}) -> {taxon} [{level}]")
        if taxon:
            labels.append(build_label(cfg, global_key, result["crop_box"], taxon))
    logger.info(f"Taxon matches: {counts}")

    if args.test:
        logger.info("Test mode, nothing uploaded to Labelbox.")
        return

    job = lb.MALPredictionImport.create_from_objects(
        client=client, project_id=project.uid,
        name=f"plantnet_{args.mission}_{uuid.uuid4().hex[:8]}", predictions=labels,
    )
    job.wait_till_done()
    if job.errors:
        logger.error(f"MAL import errors: {job.errors[:5]}")
        sys.exit(1)
    logger.info(f"{len(labels)} Pl@ntNet prelabels imported into {project.name}.")


if __name__ == "__main__":
    main()
