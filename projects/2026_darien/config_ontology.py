INPUT_CSV        = "projects/2026_darien/Especies_Darien_JV_2026_05_11_WCVP13_GBIF.csv"

CSV_DELIMITER    = ","
CSV_ENCODING     = "utf-8-sig"

COL_BINOMIAL     = "orig_name"
COL_CODE1        = "sp6"   # appended to label; set to None to omit
COL_CODE2        = None    # appended to label; set to None to omit
COL_GENUS        = "wcvp_matched_name"
COL_FAMILY       = "wcvp_accepted_family"
COL_GBIF_ID      = "wcvp_matched_name_gbif_id"

LABEL_SEPARATOR  = "-"

ONTOLOGY_NAME    = "2026_darien_planta"
BBOX_TOOL_NAME   = "Planta"
TAXON_CLASS_NAME = "Taxón"
ORGAN_CLASS_NAME = "Órgano"

ORGAN_OPTIONS    = [
    ("flor",   "Flor"),
    ("fruto",  "Fruto"),
]

OUTPUT_DIR       = "projects/2026_darien"
GBIF_CACHE_FILE  = "projects/2026_darien/cache/gbif_ontology_cache.json"

GBIF_MATCH_URL   = "https://api.gbif.org/v1/species/match"
GBIF_MAX_RETRIES = 3
GBIF_PHYLUM      = "Tracheophyta"
