INPUT_CSV        = "projects/2025_wa_roberge/species_list_final.csv"

CSV_DELIMITER    = ","
CSV_ENCODING     = "utf-8-sig"

COL_BINOMIAL     = "wcvp_accepted_name"
COL_CODE1        = None   # appended to label; set to None to omit
COL_CODE2        = None   # appended to label; set to None to omit
COL_GENUS        = "wcvp_accepted_name"
COL_FAMILY       = "wcvp_family"
COL_GBIF_ID      = "gbif_usage_key"

EXTRA_GENERA   = ["Hybanthus", "Macrozamia", "Microcorys", "Olearia"]

LABEL_SEPARATOR  = "-"

ONTOLOGY_NAME    = "2025_wa_roberge_plants"
BBOX_TOOL_NAME   = "Plants"
TAXON_CLASS_NAME = "Taxon"
ORGAN_CLASS_NAME = "Organ"

ORGAN_OPTIONS    = [
    ("flower",   "Flower"),
    ("fruit",  "Fruit"),
]

OUTPUT_DIR       = "projects/2025_wa_roberge"
GBIF_CACHE_FILE  = "projects/2025_wa_roberge/cache/gbif_cache.json"

GBIF_MATCH_URL   = "https://api.gbif.org/v1/species/match"
GBIF_MAX_RETRIES = 3
GBIF_PHYLUM      = "Tracheophyta"
