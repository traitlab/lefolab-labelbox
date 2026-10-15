INPUT_CSV        = "projects/2024_qc/QC_SPECIES_LIST.csv"

CSV_DELIMITER    = ";"
CSV_ENCODING     = "utf-8-sig"

# Explicit list: every row (species, genus, family) becomes an option as-is
COL_TYPE         = "type"
COL_BINOMIAL     = "name"
COL_CODE1        = None   # appended to label; set to None to omit
COL_CODE2        = None   # appended to label; set to None to omit
COL_GBIF_ID      = "gbif_id"

LABEL_SEPARATOR  = "-"

ONTOLOGY_NAME    = "2024_qc_plante"
BBOX_TOOL_NAME   = "Plante"
TAXON_CLASS_NAME = "Taxon"
ORGAN_CLASS_NAME = "Organe"

ORGAN_OPTIONS    = [
    ("fleur",  "Fleur"),
    ("fruit",  "Fruit"),
]

OUTPUT_DIR       = "projects/2024_qc"
