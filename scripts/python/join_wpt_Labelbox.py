import geopandas as gpd
import glob
import json
import numpy as np
import os
import requests
import time
import argparse
import pandas as pd
import math
from pygbif import species
from dotenv import load_dotenv
from datetime import datetime
from io import BytesIO
from PIL import Image

load_dotenv()

# ---- GBIF helpers ----

def load_cache(path):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return {str(k): v for k, v in json.load(f).items()}
    except FileNotFoundError:
        return {}
    except Exception as e:
        print(f"[WARN] Could not load cache: {e}")
        return {}

def save_cache(cache, path):
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(cache, f, ensure_ascii=False)
    except Exception as e:
        print(f"[WARN] Could not save cache: {e}")

def get_gbif_taxonomy_cached(gbif_id, cache, retries=3, backoff=1.5):
    if gbif_id is None or (isinstance(gbif_id, float) and math.isnan(gbif_id)):
        return {}
    key = str(int(gbif_id)) if str(gbif_id).replace(".", "").isdigit() else str(gbif_id)
    if key in cache:
        return cache[key] or {}
    for attempt in range(retries):
        try:
            info = species.name_usage(key=key)
            out = {
                "scientificName": info.get("scientificName"),
                "canonicalName":  info.get("canonicalName"),
                "rank":           info.get("rank"),
                "genus":          info.get("genus"),
                "family":         info.get("family"),
            }
            cache[key] = out
            return out
        except Exception as e:
            if attempt == retries - 1:
                print(f"[GBIF] Failed for {key}: {e}")
                cache[key] = {}
                return {}
            time.sleep(backoff ** attempt)

# ---- Labelbox mask download ----

def retrieve_mask_array(mask_url, api_key):
    headers = {"Authorization": f"Bearer {api_key}"}
    for attempt in range(3):
        try:
            response = requests.get(mask_url, headers=headers)
            if response.status_code == 401:
                print(f"Auth failed: {mask_url}")
                return None
            if response.status_code == 403:
                print(f"Forbidden: {mask_url}")
                return None
            if response.status_code != 200:
                if attempt < 2:
                    time.sleep(2)
                    continue
                return None
            content_type = response.headers.get("content-type", "").lower()
            if "image" not in content_type:
                print(f"Expected image, got {content_type}: {mask_url}")
                return None
            mask_image = Image.open(BytesIO(response.content))
            return np.array(mask_image.convert("L"))
        except Exception as e:
            if attempt < 2:
                time.sleep(2)
            else:
                print(f"Error downloading mask: {e}")
                return None

# ---- Species column builder ----

def build_species_columns(annotations, tree_gbif_ids):
    """
    annotations: list of dicts with keys: plant_name, plant_value (gbif_id str),
                 mask_coverage (float or None), mask_path (str or None),
                 canonicalName, family
    tree_gbif_ids: set of str gbif IDs that are trees

    Returns a flat dict of wide columns:
      tree_A_lb_label, tree_A_gbif_id, tree_A_name, tree_A_cov, tree_A_family, tree_A_masks
      tree_B_*, ...
      other_a_lb_label, other_a_gbif_id, other_a_name, other_a_cov, other_a_family, other_a_masks
      other_b_*, ...
    tree_*_masks / other_*_masks: ";"-joined relative paths of that species' mask files.
    """
    # Aggregate same species (sum coverage)
    by_species = {}
    for ann in annotations:
        key = ann["plant_value"]
        if not key or key in ("Null", "None", ""):
            continue
        if key not in by_species:
            by_species[key] = {
                "plant_name": ann["plant_name"],
                "plant_value": ann["plant_value"],
                "canonicalName": ann.get("canonicalName"),
                "family": ann.get("family"),
                "mask_coverage": 0.0,
                "masks": [],
            }
        cov = ann.get("mask_coverage")
        if cov is not None:
            try:
                by_species[key]["mask_coverage"] += float(cov)
            except (ValueError, TypeError):
                pass
        if ann.get("mask_path"):
            by_species[key]["masks"].append(ann["mask_path"])

    trees = sorted(
        [v for k, v in by_species.items() if k in tree_gbif_ids],
        key=lambda x: x["mask_coverage"],
        reverse=True,
    )
    others = sorted(
        [v for k, v in by_species.items() if k not in tree_gbif_ids],
        key=lambda x: x["mask_coverage"],
        reverse=True,
    )

    row = {}
    for i, sp in enumerate(trees):
        suffix = chr(ord("A") + i)
        prefix = f"tree_{suffix}"
        row[f"{prefix}_lb_label"]  = sp["plant_name"]
        row[f"{prefix}_gbif_id"]   = sp["plant_value"]
        row[f"{prefix}_name"]      = sp["canonicalName"]
        row[f"{prefix}_cov"]       = round(sp["mask_coverage"], 2)
        row[f"{prefix}_family"]    = sp["family"]
        row[f"{prefix}_masks"]     = ";".join(sp["masks"])

    for i, sp in enumerate(others):
        suffix = chr(ord("a") + i)
        prefix = f"other_{suffix}"
        row[f"{prefix}_lb_label"]  = sp["plant_name"]
        row[f"{prefix}_gbif_id"]   = sp["plant_value"]
        row[f"{prefix}_name"]      = sp["canonicalName"]
        row[f"{prefix}_cov"]       = round(sp["mask_coverage"], 2)
        row[f"{prefix}_family"]    = sp["family"]
        row[f"{prefix}_masks"]     = ";".join(sp["masks"])

    return row


def join_gpkg_with_json(
    gpkg_path, json_path, output_path=None,
    labelbox_api_key=None, csv_filter_path=None,
    masks_path=None, cache_path=None, debug=False
):
    start_time = datetime.now()
    print(f"Processing started at: {start_time.strftime('%Y-%m-%d %H:%M:%S')}")

    gdf = gpd.read_file(gpkg_path)
    print(f"GPKG shape: {gdf.shape}")

    # Load tree species set
    tree_gbif_ids = set()
    if csv_filter_path:
        try:
            csv_df = pd.read_csv(csv_filter_path)
            tree_gbif_ids = set(csv_df["gbif_taxonID"].astype(str))
            print(f"Loaded {len(tree_gbif_ids)} tree taxon IDs from {csv_filter_path}")
        except Exception as e:
            print(f"Warning: Could not load CSV filter file: {e}")

    # Load GBIF cache once
    gbif_cache = load_cache(cache_path)

    # Read JSON export (one record per line)
    json_data = []
    with open(json_path, "r", encoding="utf-8") as f:
        for line in f:
            try:
                json_data.append(json.loads(line.strip()))
            except json.JSONDecodeError:
                continue
    print(f"JSON records: {len(json_data)}")

    output_dir = output_path or "."
    os.makedirs(output_dir, exist_ok=True)

    # Parse JSON into per-image records with annotation lists
    image_records = []

    for record in json_data:
        data_row = record.get("data_row", {})
        tele_url = data_row.get("row_data", "")
        global_key = data_row.get("global_key", "")
        data_row_id = data_row.get("id", "")
        dataset_id = data_row.get("details", {}).get("dataset_id", "")
        dataset_name = data_row.get("details", {}).get("dataset_name", "")

        for project_id, project_data in record.get("projects", {}).items():
            project_name = project_data.get("name", "")
            workflow_status = project_data.get("project_details", {}).get("workflow_status", "")
            selected_label_id = project_data.get("project_details", {}).get("selected_label_id")

            labels = project_data.get("labels", [])
            if not labels:
                continue

            # Pick winner label: selected > latest valid > skip
            valid_labels = [
                l for l in labels
                if not l.get("performance_details", {}).get("skipped", False)
                and l.get("annotations", {}).get("objects")
            ]
            if not valid_labels:
                continue

            if selected_label_id:
                winner = next((l for l in valid_labels if l["id"] == selected_label_id), None)
                if winner is None:
                    winner = max(valid_labels, key=lambda l: l.get("label_details", {}).get("created_at", ""))
            else:
                winner = max(valid_labels, key=lambda l: l.get("label_details", {}).get("created_at", ""))

            # Extract all classified annotations from the winner label
            annotations = []
            for ann in winner.get("annotations", {}).get("objects", []):
                plant_name = ""
                plant_value = ""
                for cls in ann.get("classifications", []):
                    for answer in cls.get("checklist_answers", []):
                        plant_name = answer.get("name", "")
                        plant_value = answer.get("value", "")
                        break
                    if plant_name:
                        break

                if not plant_value or plant_value in ("Null", "None"):
                    continue

                mask_coverage = None
                mask_path = None

                mask_info = ann.get("mask", {})
                if mask_info and "url" in mask_info and masks_path:
                    mask_url = mask_info["url"]
                    # Use the annotation's feature_id for a stable, species-specific filename
                    feature_id = ann.get("feature_id", "")
                    mask_filename = f"mask_{feature_id}.png"
                    full_mask_path = os.path.join(masks_path, mask_filename)
                    mask_rel_path = f"cache/masks/{mask_filename}"

                    if os.path.exists(full_mask_path):
                        mask_path = mask_rel_path
                        try:
                            mask_data = np.array(Image.open(full_mask_path).convert("L"))
                            total_pixels = mask_data.size
                            masked_pixels = np.sum(mask_data > 128)
                            mask_coverage = round((masked_pixels / total_pixels) * 100, 2)
                        except Exception:
                            pass
                    elif labelbox_api_key:
                        try:
                            mask_data = retrieve_mask_array(mask_url, labelbox_api_key)
                            if mask_data is not None:
                                Image.fromarray(mask_data).save(full_mask_path)
                                mask_path = mask_rel_path
                                total_pixels = mask_data.size
                                masked_pixels = np.sum(mask_data > 128)
                                mask_coverage = round((masked_pixels / total_pixels) * 100, 2)
                        except Exception:
                            pass

                # GBIF taxonomy lookup
                taxonomy = get_gbif_taxonomy_cached(plant_value, gbif_cache)

                annotations.append({
                    "plant_name":    plant_name,
                    "plant_value":   plant_value,
                    "mask_coverage": mask_coverage,
                    "mask_path":     mask_path,
                    "canonicalName": taxonomy.get("canonicalName"),
                    "family":        taxonomy.get("family"),
                })

            if not annotations:
                continue

            species_cols = build_species_columns(annotations, tree_gbif_ids)

            image_records.append({
                "tele_url":        tele_url,
                "data_row_id":     data_row_id,
                "global_key":      global_key,
                "dataset_id":      dataset_id,
                "dataset_name":    dataset_name,
                "project_id":      project_id,
                "project_name":    project_name,
                "workflow_status": workflow_status,
                "label_id":        winner["id"],
                "labeled_by":      winner.get("label_details", {}).get("created_by", ""),
                "label_created_at": winner.get("label_details", {}).get("created_at", ""),
                "annotation_count": len(annotations),
                "tree_species_count": len({a["plant_value"] for a in annotations if a["plant_value"] in tree_gbif_ids}),
                **species_cols,
            })

    save_cache(gbif_cache, cache_path)

    if not image_records:
        print("[WARN] No labeled images found.")
        return None

    result_df = pd.DataFrame(image_records)

    # Order species columns: all tree_* (A, B, ...) then all other_* (a, b, ...),
    # keeping each species' fields in a fixed order. Meta columns stay first.
    field_order = ["lb_label", "gbif_id", "name", "cov", "family", "masks"]

    def is_species(col):
        parts = col.split("_", 2)
        return parts[0] in ("tree", "other") and len(parts) == 3 and parts[2] in field_order

    def species_key(col):
        prefix, suffix, field = col.split("_", 2)
        return (0 if prefix == "tree" else 1, suffix, field_order.index(field))

    meta_cols = [c for c in result_df.columns if not is_species(c)]
    species_cols_ordered = sorted([c for c in result_df.columns if is_species(c)], key=species_key)
    result_df = result_df[meta_cols + species_cols_ordered]
    print(f"Labeled images: {len(result_df)}")

    # Merge onto GPKG (one row per GPKG point; points without labeled species are dropped)
    joined_gdf = gdf.merge(result_df, on="tele_url", how="inner")
    joined_gdf["wide_image"] = joined_gdf["wide_url"].astype(str).str.split("/").str[-1]
    joined_gdf["base_image"] = joined_gdf["global_key"].fillna(
        joined_gdf["tele_url"].astype(str).str.split("/").str[-1]
    )

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    if debug:
        json_basename = os.path.splitext(os.path.basename(json_path))[0]
        csv_path = os.path.join(output_dir, f"{json_basename}_processed_{timestamp}.csv")
        result_df.to_csv(csv_path, index=False)
        print(f"Debug CSV saved to: {csv_path}")

    gpkg_basename = os.path.splitext(os.path.basename(gpkg_path))[0]
    gpkg_filename = os.path.join(output_dir, f"{gpkg_basename}_joined_{timestamp}.gpkg")
    joined_gdf.to_file(gpkg_filename, driver="GPKG")

    end_time = datetime.now()
    print(f"Original GPKG records: {len(gdf)}")
    print(f"Output records (matched + labeled): {len(joined_gdf)}")
    print(f"Processing completed at: {end_time.strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"Total time: {end_time - start_time}")
    print(f"Output saved to: {gpkg_filename}")

    return joined_gdf


def find_one_input(input_dir, ext, must_contain):
    """Return the single *<ext> file in input_dir whose name contains must_contain.

    Errors out if zero or more than one file matches, to avoid picking the wrong input.
    """
    matches = sorted(
        f for f in glob.glob(os.path.join(input_dir, f"*{ext}"))
        if must_contain.lower() in os.path.basename(f).lower()
    )
    if len(matches) == 1:
        return matches[0]
    names = ", ".join(os.path.basename(m) for m in matches) or "<none>"
    raise SystemExit(
        f"[ERROR] Expected exactly one *{ext} file containing '{must_contain}' "
        f"in {input_dir}, found {len(matches)}: {names}"
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Create GeoDataFrame for Labelbox annotations",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""\
Example (run from repo root):
  python scripts/python/join_wpt_Labelbox.py --project projects/2024_bci --debug

Everything is derived from --project:
  <project>/input    one *.gpkg ("wpt"), one *.json ("labelbox"), one *.csv ("trees")
  <project>/output   joined GPKG, optional debug CSV
  <project>/cache    gbif_annotation_cache.json (GBIF id -> taxonomy), masks/
""",
    )
    parser.add_argument("--project", type=str, required=True,
                        help="Project directory, e.g. projects/2024_bci")
    parser.add_argument("--debug", action="store_true",
                        help="Also write the intermediate processed CSV")
    args = parser.parse_args()

    input_dir  = os.path.join(args.project, "input")
    output_dir = os.path.join(args.project, "output")
    cache_dir  = os.path.join(args.project, "cache")

    gpkg_path       = find_one_input(input_dir, ".gpkg", "wpt")
    json_path       = find_one_input(input_dir, ".json", "labelbox")
    csv_filter_path = find_one_input(input_dir, ".csv", "trees")
    print(f"gpkg:   {gpkg_path}")
    print(f"json:   {json_path}")
    print(f"filter: {csv_filter_path}")

    os.makedirs(cache_dir, exist_ok=True)
    masks_path = os.path.join(cache_dir, "masks")
    os.makedirs(masks_path, exist_ok=True)

    cache_path = os.path.join(cache_dir, "gbif_annotation_cache.json")

    labelbox_api_key = os.getenv("LABELBOX_API_KEY")
    if labelbox_api_key:
        print("Labelbox API key loaded.")
    else:
        print("[WARN] LABELBOX_API_KEY not found — masks will not be downloaded.")

    join_gpkg_with_json(
        gpkg_path, json_path, output_dir,
        labelbox_api_key, csv_filter_path,
        masks_path, cache_path,
        debug=args.debug,
    )
