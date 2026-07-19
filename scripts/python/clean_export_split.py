"""Clean a Labelbox export into a training-ready JSON and assign a train/val/test split.

Input: the NDJSON Labelbox export (one data row per line). Only photos whose
workflow_status is DONE are kept.

Output (JSON array, one object per photo):
    {
      "url": "<source image URL>",
      "filename": "<image basename>",
      "mission": "<mission metadata>",
      "width": <px>, "height": <px>,
      "boxes": [{"top", "left", "height", "width",   # Labelbox-native pixels
                 "taxon": "<name>", "gbif_id": "<id>"}, ...],
      "main_species": "<taxon with the largest summed box area in the photo>",
      "split": "train" | "valid" | "test"
    }

Split rules:
  - One photo -> exactly one split (all boxes of a photo stay together).
  - Photos whose drone footprints overlap are grouped and kept in the same split
    (connected components over footprint intersection). The split is assigned at
    the group level.
  - Stratified random split, strata = the group's main species (taxon with the
    largest summed box area over the whole group). Target 70/15/15.
  - Every species must appear in test, even if that leaves it absent from
    train/val (species with a single photo or a single overlap group).

Usage:
    python clean_export_split.py \
        --export /data/sharing/labelbox/exports/2025_wa_roberge.json \
        --footprints projects/2025_wa_roberge/2025_wa_roberge_footprints.gpkg \
        --output projects/2025_wa_roberge/2025_wa_roberge_dataset.json
"""

import argparse
import json
import os
import random
from collections import defaultdict

import geopandas as gpd

from _common import setup_logging

logger = setup_logging()

TRAIN_FRAC, VAL_FRAC = 0.70, 0.15  # test gets the remainder
SEED = 13

parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
parser.add_argument("--export", required=True, help="Labelbox NDJSON export")
parser.add_argument("--footprints", required=True, help="GeoPackage of photo footprints (needs a 'filename' column)")
parser.add_argument("--output", required=True, help="Output JSON file (array of photos)")
args = parser.parse_args()


def load_done_photos(path):
    """Parse the NDJSON export, keeping only DONE photos.

    Returns a list of photo dicts with url, filename, mission, dimensions, and
    boxes. ``main_species`` is filled in afterwards by ``assign_main_species``.
    """
    photos = []
    for line in open(path, encoding="utf-8"):
        line = line.strip()
        if not line:
            continue
        row = json.loads(line)

        for project in row["projects"].values():
            details = project.get("project_details", {})
            if details.get("workflow_status") != "DONE":
                continue

            url = row["data_row"]["row_data"]
            media = row.get("media_attributes", {})
            mission = next((m["value"] for m in row.get("metadata_fields", [])
                            if m["schema_name"] == "mission"), None)

            boxes = []
            area_by_taxon = defaultdict(float)
            for label in project.get("labels", []):
                for obj in label["annotations"]["objects"]:
                    bb = obj.get("bounding_box")
                    if not bb:
                        continue
                    taxon = gbif_id = None
                    for clf in obj.get("classifications", []):
                        answer = clf.get("radio_answer")
                        if answer:
                            taxon, gbif_id = answer["name"], answer["value"]
                    boxes.append({"top": bb["top"], "left": bb["left"],
                                  "height": bb["height"], "width": bb["width"],
                                  "taxon": taxon, "gbif_id": gbif_id})
                    if taxon is not None:
                        area_by_taxon[taxon] += bb["height"] * bb["width"]

            if not area_by_taxon:
                logger.warning(f"DONE photo with no taxon-labelled box, skipped: {url}")
                continue

            photos.append({
                "url": url,
                "filename": os.path.basename(url),
                "mission": mission,
                "width": media.get("width"),
                "height": media.get("height"),
                "boxes": boxes,
                "_area_by_taxon": dict(area_by_taxon),  # dropped before writing
            })
    return photos


def assign_main_species(photos):
    """Set each photo's ``main_species`` = taxon with the largest summed box area.

    Ties (equal summed area) are broken toward the globally rarer taxon (fewer
    photos), then alphabetically, so the tie favours coverage of rare species and
    stays deterministic across projects.
    """
    photo_count = defaultdict(int)  # photos containing each taxon (any box)
    for photo in photos:
        for taxon in photo["_area_by_taxon"]:
            photo_count[taxon] += 1

    for photo in photos:
        # Rank by summed area desc, then rarity asc (fewer photos), then name asc.
        photo["main_species"] = max(
            photo["_area_by_taxon"],
            key=lambda t: (photo["_area_by_taxon"][t], -photo_count[t], _reverse_key(t)),
        )


def _reverse_key(name):
    """Sort key so that ``max`` prefers the alphabetically-first name on a full tie."""
    return tuple(-ord(c) for c in name)


def group_overlapping(photos, footprints_path):
    """Assign a group id so photos with overlapping footprints share a group.

    Groups are connected components of the "footprints intersect" graph, computed
    with a spatial join on the projected footprint geometries. Photos without a
    footprint form their own singleton group.
    """
    gdf = gpd.read_file(footprints_path)
    if gdf["filename"].duplicated().any():
        raise ValueError("Duplicate 'filename' values in the footprints GeoPackage.")

    filenames = [p["filename"] for p in photos]
    sub = gdf[gdf["filename"].isin(filenames)].copy()
    matched = set(sub["filename"])
    missing = [fn for fn in filenames if fn not in matched]
    if missing:
        logger.warning(f"{len(missing)} DONE photo(s) have no footprint; each is its own group. e.g. {missing[:3]}")

    sub = sub.reset_index(drop=True)
    idx_of = {fn: i for i, fn in enumerate(sub["filename"])}

    # Union-Find over footprint pairs that intersect.
    parent = list(range(len(sub)))

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(a, b):
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[ra] = rb

    pairs = gpd.sjoin(sub[["filename", "geometry"]], sub[["filename", "geometry"]],
                      how="inner", predicate="intersects")
    for left, right in zip(pairs.index, pairs["index_right"]):
        if left != right:
            union(left, right)

    group_of = {}
    for photo in photos:
        fn = photo["filename"]
        if fn in idx_of:
            group_of[fn] = f"g{find(idx_of[fn])}"
        else:
            group_of[fn] = f"solo:{fn}"  # no footprint -> singleton
    return group_of


def stratified_split(photos, group_of):
    """Assign train/val/test at the group level, stratified by group main species.

    Groups spill 70/15/15 per main species to approach the target. Then a coverage
    pass guarantees that *every species present in any box* appears in at least one
    test photo, moving a group that contains it into test when needed.
    """
    # Build groups: their main species (largest summed area) and all present species.
    group_area = defaultdict(lambda: defaultdict(float))
    group_species = defaultdict(set)  # every taxon present in the group (any box)
    for photo in photos:
        g = group_of[photo["filename"]]
        for taxon, area in photo["_area_by_taxon"].items():
            group_area[g][taxon] += area
            group_species[g].add(taxon)
    group_main = {g: max(areas, key=areas.get) for g, areas in group_area.items()}

    # Bucket group ids by their main species, shuffled deterministically.
    rng = random.Random(SEED)
    by_species = defaultdict(list)
    for g, species in group_main.items():
        by_species[species].append(g)

    split_of_group = {}
    for species, groups in by_species.items():
        rng.shuffle(groups)
        n = len(groups)
        # Always seed one group into test first (guarantees test coverage).
        split_of_group[groups[0]] = "test"
        rest = groups[1:]
        # Split the remainder 70/15/15; val before test so small counts still fill val.
        n_train = round(n * TRAIN_FRAC)
        n_val = round(n * VAL_FRAC)
        for i, g in enumerate(rest):
            if i < n_train:
                split_of_group[g] = "train"
            elif i < n_train + n_val:
                split_of_group[g] = "valid"
            else:
                split_of_group[g] = "test"

    # Coverage pass: every species present in any box must be in a test group.
    in_test = set()
    for g, s in split_of_group.items():
        if s == "test":
            in_test |= group_species[g]
    all_present = set().union(*group_species.values())
    for species in sorted(all_present - in_test):
        # Move the smallest group containing this species into test (fewest photos
        # pulled out of train/val), preferring groups not already anchoring coverage.
        candidates = [g for g in group_species if species in group_species[g] and split_of_group[g] != "test"]
        chosen = min(candidates, key=lambda g: len(group_species[g]))
        split_of_group[chosen] = "test"
        in_test |= group_species[chosen]

    return {photo["filename"]: split_of_group[group_of[photo["filename"]]] for photo in photos}


photos = load_done_photos(args.export)
logger.info(f"{len(photos)} DONE photos loaded")

assign_main_species(photos)

group_of = group_overlapping(photos, args.footprints)
logger.info(f"{len(set(group_of.values()))} overlap groups")

split_of = stratified_split(photos, group_of)
counts = defaultdict(int)
for s in split_of.values():
    counts[s] += 1
logger.info(f"Split: {dict(counts)}")

# Verify: every species present in any box appears in a test photo.
species_in_test = set()
all_species = set()
for p in photos:
    present = set(p["_area_by_taxon"])
    all_species |= present
    if split_of[p["filename"]] == "test":
        species_in_test |= present
missing_test = all_species - species_in_test
if missing_test:
    logger.warning(f"{len(missing_test)} species missing from test: {sorted(missing_test)}")
else:
    logger.info(f"All {len(all_species)} species represented in test")

for photo in photos:
    photo["split"] = split_of[photo["filename"]]
    del photo["_area_by_taxon"]

with open(args.output, "w", encoding="utf-8") as f:
    json.dump(photos, f, ensure_ascii=False, indent=2)
logger.info(f"Wrote {len(photos)} photos to {args.output}")
