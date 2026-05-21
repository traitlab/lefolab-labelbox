import argparse
import labelbox as lb
import logging
import os
import sys

from dotenv import load_dotenv
from pathlib import Path

# Setup logging with timestamp
logger = logging.getLogger()
logger.setLevel(logging.INFO)

# Handler for INFO to stdout
stdout_handler = logging.StreamHandler(sys.stdout)
stdout_handler.setLevel(logging.INFO)
stdout_handler.addFilter(lambda record: record.levelno == logging.INFO)
stdout_handler.setFormatter(logging.Formatter('[%(asctime)s] %(message)s', datefmt='%Y-%m-%d %H:%M:%S'))

# Handler for WARNING and ERROR to stderr
stderr_handler = logging.StreamHandler(sys.stderr)
stderr_handler.setLevel(logging.WARNING)
stderr_handler.setFormatter(logging.Formatter('[%(asctime)s] %(message)s', datefmt='%Y-%m-%d %H:%M:%S'))

# Remove default handlers and add custom ones
logger.handlers = []
logger.addHandler(stdout_handler)
logger.addHandler(stderr_handler)

# Load environment variables from .env file
project_root = Path(__file__).parent.parent.parent
load_dotenv(dotenv_path=project_root / '.env')

LABELBOX_API_KEY = os.getenv("LABELBOX_API_KEY")
if not LABELBOX_API_KEY:
    logger.error("LABELBOX_API_KEY environment variable is not set")
    raise ValueError("LABELBOX_API_KEY environment variable is not set")

client = lb.Client(api_key=LABELBOX_API_KEY)

parser = argparse.ArgumentParser(description="Send data rows to Labelbox project for annotation.")
parser.add_argument("--mission_id", required=True, help="Mission ID to create a batch for annotation.")
parser.add_argument("--project", required=True, help="Project/dataset name.")
args = parser.parse_args()

mission_id = args.mission_id
project_name = args.project

# Find dataset (name = project_name)
datasets = client.get_datasets()
dataset = next((ds for ds in datasets if ds.name == project_name), None)
if not dataset:
    logger.error(f"Dataset '{project_name}' not found.")
    sys.exit(1)

# Find the Labelbox annotation project (name = project_name)
projects = client.get_projects()
lb_project = next((p for p in projects if p.name == project_name), None)
if not lb_project:
    logger.error(f"Labelbox project '{project_name}' not found.")
    sys.exit(1)

# Export data rows from the dataset with metadata, then filter by mission_id
data_row_ids = []

def handle_export(output: lb.BufferedJsonConverterOutput):
    row = output.json
    metadata_fields = row.get("metadata_fields", [])
    if any(mf.get("name") == "mission" and mf.get("value") == mission_id
           for mf in metadata_fields):
        data_row_ids.append(row["data_row"]["id"])

export_task = dataset.export_v2(params={"metadata_fields": True})
export_task.wait_till_done()

if export_task.errors:
    logger.error(f"Export errors: {export_task.errors}")
    sys.exit(1)

export_task.get_buffered_stream(stream_type=lb.StreamType.RESULT).start(
    stream_handler=handle_export
)

if not data_row_ids:
    logger.error(f"No data rows found for mission '{mission_id}' in dataset '{project_name}'.")
    sys.exit(1)

logger.info(f"{len(data_row_ids)} data rows found for mission '{mission_id}' in dataset '{project_name}'.")

# Create batch in the annotation project
batch = lb_project.create_batch(
    name=mission_id,
    data_rows=data_row_ids,
    priority=3
)
logger.info(f"Batch created for {mission_id}: {batch.name}")
