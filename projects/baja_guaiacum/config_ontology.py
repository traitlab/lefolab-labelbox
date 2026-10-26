INPUT_CSV        = "projects/baja_guaiacum/Species_List_Boca_Vinoramas_2026_WCVP13_GBIF.csv"

CSV_DELIMITER    = ","
CSV_ENCODING     = "utf-8-sig"

COL_BINOMIAL     = "orig_name"
COL_CODE1        = None   # appended to label; set to None to omit
COL_CODE2        = None    # appended to label; set to None to omit
LABEL_SEPARATOR  = "-"
COL_GENUS        = "wcvp_matched_name"
COL_FAMILY       = "wcvp_accepted_family"
COL_GBIF_ID      = "wcvp_matched_name_gbif_id"

ONTOLOGY_NAME    = "baja_guaiacum_planta"
BBOX_TOOL_NAME   = "Planta"
TAXON_CLASS_NAME = "Taxón"
ORGAN_CLASS_NAME = "Órgano"

ORGAN_OPTIONS    = [
    ("flor",   "Flor"),
    ("fruto",  "Fruto"),
]

OUTPUT_DIR       = "projects/baja_guaiacum"
GBIF_CACHE_FILE  = "projects/baja_guaiacum/cache/gbif_ontology_cache.json"

GBIF_MATCH_URL   = "https://api.gbif.org/v2/species/match"
GBIF_CHECKLIST_KEY = "7ddf754f-d193-4cc9-b351-99906754a03b"  # COL XR
GBIF_MAX_RETRIES = 3
GBIF_PHYLUM      = "Tracheophyta"
