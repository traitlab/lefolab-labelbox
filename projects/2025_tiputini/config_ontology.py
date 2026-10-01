INPUT_CSV        = "projects/2025_tiputini/TBS_SPECIES_LIST.csv"

CSV_DELIMITER    = ";"
CSV_ENCODING     = "utf-8-sig"

# Explicit list: every row (species, genus, family) becomes an option as-is
COL_TYPE         = "type"
COL_BINOMIAL     = "name"
COL_CODE1        = "code1"   # appended to label; set to None to omit
COL_CODE2        = "code2"   # appended to label; set to None to omit
COL_GBIF_ID      = "gbif_id"

LABEL_SEPARATOR  = "-"

ONTOLOGY_NAME    = "2025_tiputini_planta"
BBOX_TOOL_NAME   = "Planta"
TAXON_CLASS_NAME = "Taxón"
ORGAN_CLASS_NAME = "Órgano"

ORGAN_OPTIONS    = [
    ("flor",   "Flor"),
    ("fruto",  "Fruto"),
]

OUTPUT_DIR       = "projects/2025_tiputini"
