import argparse
import csv
import importlib.util
import json
import labelbox as lb
import logging
import os
import requests
import sys

from dotenv import load_dotenv
from pathlib import Path

project_root = Path(__file__).parent.parent.parent
load_dotenv(dotenv_path=project_root / ".env")

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


def load_config(path):
    spec = importlib.util.spec_from_file_location("config", path)
    cfg = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(cfg)
    return cfg


def load_cache(cache_path: Path) -> dict:
    if cache_path.exists():
        with open(cache_path, encoding="utf-8") as f:
            return json.load(f)
    return {}


def save_cache(cache: dict, cache_path: Path) -> None:
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    with open(cache_path, "w", encoding="utf-8") as f:
        json.dump(cache, f, ensure_ascii=False, indent=2)


def resolve_gbif(name: str, rank: str, cache: dict, cfg) -> int | None:
    key = f"{name}|{rank}"
    if key in cache:
        return cache[key]

    params = {"name": name, "rank": rank}
    if cfg.GBIF_PHYLUM:
        params["phylum"] = cfg.GBIF_PHYLUM

    for attempt in range(cfg.GBIF_MAX_RETRIES):
        try:
            r = requests.get(cfg.GBIF_MATCH_URL, params=params, timeout=10)
            r.raise_for_status()
            data = r.json()
            if data.get("matchType") == "EXACT":
                usage_key = data.get("usageKey")
                cache[key] = usage_key
                return usage_key
            else:
                cache[key] = None
                return None
        except Exception as e:
            if attempt == cfg.GBIF_MAX_RETRIES - 1:
                logger.warning(f"GBIF lookup failed for {name} ({rank}) after {cfg.GBIF_MAX_RETRIES} attempts: {e}")
    cache[key] = None
    return None


def build_species_label(row: dict, cfg) -> str:
    parts = [row[cfg.COL_BINOMIAL]]
    for col in (cfg.COL_CODE1, cfg.COL_CODE2):
        if col is not None:
            code = row.get(col, "").strip().upper()
            if code:
                parts.append(code)
    return cfg.LABEL_SEPARATOR.join(parts)


def load_species_rows(cfg) -> list[dict]:
    csv_path = project_root / cfg.INPUT_CSV
    rows = []
    with open(csv_path, encoding=cfg.CSV_ENCODING, newline="") as f:
        reader = csv.DictReader(f, delimiter=cfg.CSV_DELIMITER)
        for row in reader:
            rows.append(row)
    return rows


def extract_genera(rows: list[dict], cfg) -> list[str]:
    seen = set()
    genera = []
    for row in rows:
        col_genus = row.get(cfg.COL_GENUS, "").strip()
        if not col_genus:
            continue
        genus = col_genus.split()[0]
        if genus not in seen:
            seen.add(genus)
            genera.append(genus)
    for genus in getattr(cfg, "EXTRA_GENERA", []):
        genus = genus.strip()
        if genus and genus not in seen:
            seen.add(genus)
            genera.append(genus)
    return sorted(genera)


def extract_families(rows: list[dict], cfg) -> list[str]:
    seen = set()
    families = []
    for row in rows:
        col_family = row.get(cfg.COL_FAMILY, "").strip()
        if col_family and col_family not in seen:
            seen.add(col_family)
            families.append(col_family)
    for family in getattr(cfg, "EXTRA_FAMILIES", []):
        family = family.strip()
        if family and family not in seen:
            seen.add(family)
            families.append(family)
    return sorted(families)


def prompt_manual_gbif_id(name: str, rank: str, suggestion: str | None, cache: dict) -> int | None:
    hint = f" (e.g. search GBIF for '{suggestion}')" if suggestion else ""
    print(f"\n  No exact GBIF match for {rank.lower()} '{name}'{hint}.")
    while True:
        try:
            raw = input(f"  Enter GBIF ID for '{name}': ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            return None
        if raw.isdigit():
            gbif_id = int(raw)
            cache[f"{name}|{rank}"] = gbif_id
            return gbif_id
        print("  Invalid input — please enter a numeric ID.")


def build_taxon_options(rows: list[dict], cache: dict, cfg) -> list[dict]:
    options = []
    seen_ids = set()

    # Species
    for row in rows:
        gbif_id_raw = row.get(cfg.COL_GBIF_ID, "").strip()
        if not gbif_id_raw:
            logger.warning(f"Missing {cfg.COL_GBIF_ID} for row: {row.get(cfg.COL_BINOMIAL, '?')} — skipping")
            continue
        try:
            gbif_id = int(gbif_id_raw)
        except ValueError:
            logger.warning(f"Non-integer {cfg.COL_GBIF_ID} '{gbif_id_raw}' for {row.get(cfg.COL_BINOMIAL, '?')} — skipping")
            continue
        if gbif_id in seen_ids:
            continue
        seen_ids.add(gbif_id)
        options.append({"type": "species", "label": build_species_label(row, cfg), "value": str(gbif_id)})

    # Genera
    for genus in extract_genera(rows, cfg):
        gbif_id = resolve_gbif(genus, "GENUS", cache, cfg)
        if gbif_id is None:
            example = next(
                (r[cfg.COL_BINOMIAL] for r in rows if r.get(cfg.COL_GENUS, "").split()[0:1] == [genus]),
                None,
            )
            gbif_id = prompt_manual_gbif_id(genus, "GENUS", example, cache)
        if gbif_id is None:
            continue
        if gbif_id in seen_ids:
            logger.warning(f"Genus '{genus}' GBIF ID {gbif_id} already used by a species entry — adding also as genus")
        seen_ids.add(gbif_id)
        options.append({"type": "genus", "label": genus, "value": str(gbif_id)})

    # Families
    for family in extract_families(rows, cfg):
        gbif_id = resolve_gbif(family, "FAMILY", cache, cfg)
        if gbif_id is None:
            example = next(
                (r[cfg.COL_BINOMIAL] for r in rows if r.get(cfg.COL_FAMILY, "").strip() == family),
                None,
            )
            gbif_id = prompt_manual_gbif_id(family, "FAMILY", example, cache)
        if gbif_id is None:
            continue
        if gbif_id in seen_ids:
            logger.warning(f"Family '{family}' GBIF ID {gbif_id} already used by a prior entry — adding also as family")
        seen_ids.add(gbif_id)
        options.append({"type": "family", "label": family, "value": str(gbif_id)})

    return options


def save_list(options: list[dict], cfg) -> None:
    output_path = project_root / cfg.OUTPUT_DIR
    output_path.mkdir(parents=True, exist_ok=True)
    list_file = output_path / "labelbox_list.csv"
    with open(list_file, "w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=["type", "label", "value"])
        writer.writeheader()
        writer.writerows(options)
    logger.info(f"List saved to {list_file} ({len(options)} rows)")


def build_ontology(options: list[dict], cfg) -> lb.OntologyBuilder:
    taxon_options = [lb.Option(value=o["value"], label=o["label"]) for o in options]
    organ_options = [lb.Option(value=v, label=l) for v, l in cfg.ORGAN_OPTIONS]

    tool = lb.Tool(
        tool=lb.Tool.Type.BBOX,
        name=cfg.BBOX_TOOL_NAME,
        classifications=[
            lb.Classification(
                class_type=lb.Classification.Type.RADIO,
                name=cfg.TAXON_CLASS_NAME,
                options=taxon_options,
            ),
            lb.Classification(
                class_type=lb.Classification.Type.CHECKLIST,
                name=cfg.ORGAN_CLASS_NAME,
                options=organ_options,
            ),
        ],
    )

    return lb.OntologyBuilder(tools=[tool])


def main() -> None:
    parser = argparse.ArgumentParser(description="Build a Labelbox ontology from a species CSV.")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--project", help="Project name (e.g. '2024_bci'); resolves to projects/<project>/config_ontology.py")
    group.add_argument("--config", help="Full path to a config file (fallback when --project is not used)")
    args = parser.parse_args()

    if args.project:
        config_path = project_root / "projects" / args.project / "config_ontology.py"
    else:
        config_path = Path(args.config)

    cfg = load_config(config_path)

    cache_path = project_root / cfg.GBIF_CACHE_FILE
    cache = load_cache(cache_path)

    logger.info(f"Loading species list from {cfg.INPUT_CSV}")
    rows = load_species_rows(cfg)
    logger.info(f"{len(rows)} rows loaded")

    logger.info("Resolving GBIF IDs for species, genera, and families…")
    options = build_taxon_options(rows, cache, cfg)
    save_cache(cache, cache_path)

    species_count = sum(1 for o in options if o["type"] == "species")
    genus_count = sum(1 for o in options if o["type"] == "genus")
    family_count = sum(1 for o in options if o["type"] == "family")
    logger.info(f"Taxón options: {species_count} species, {genus_count} genera, {family_count} families ({len(options)} total)")

    save_list(options, cfg)

    try:
        answer = input(f"\nReview the list above and confirm creation of '{cfg.ONTOLOGY_NAME}' in Labelbox. Type 'yes' to proceed: ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        print()
        logger.info("Aborted.")
        return

    if answer != "yes":
        logger.info("Aborted.")
        return

    LABELBOX_API_KEY = os.getenv("LABELBOX_API_KEY")
    if not LABELBOX_API_KEY:
        logger.error("LABELBOX_API_KEY environment variable is not set")
        sys.exit(1)

    client = lb.Client(api_key=LABELBOX_API_KEY)
    ontology_builder = build_ontology(options, cfg)

    logger.info(f"Creating ontology '{cfg.ONTOLOGY_NAME}' in Labelbox…")
    ontology = client.create_ontology(
        name=cfg.ONTOLOGY_NAME,
        normalized=ontology_builder.asdict(),
        media_type=lb.MediaType.Image,
    )
    logger.info(f"Ontology created: {ontology.uid}")


if __name__ == "__main__":
    main()
