import argparse
import labelbox as lb
import sys

from _common import get_client, resolve_project, setup_logging

logger = setup_logging()
client = get_client()

parser = argparse.ArgumentParser(description="Send data rows to Labelbox project for annotation.")
parser.add_argument("--mission_id", required=True, help="Mission ID to create a batch for annotation.")
parser.add_argument("--project", required=True, help="Project/dataset name.")
parser.add_argument("--workflow_step", help="Task queue name to move data rows to (e.g. 'Initial labeling task').")
parser.add_argument("--model_run_id", help="Model run ID whose predictions to send as prelabels.")
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
lb_project = resolve_project(client, project_name)

# Export data rows from the dataset with metadata, then filter by mission_id
data_row_ids = []

def handle_export(output: lb.BufferedJsonConverterOutput):
    row = output.json
    metadata_fields = row.get("metadata_fields", [])
    if any(mf.get("schema_name") == "mission" and mf.get("value") == mission_id
           for mf in metadata_fields):
        data_row_ids.append(row["data_row"]["id"])

export_task = dataset.export(params={"metadata_fields": True})
export_task.wait_till_done()

if export_task.has_errors():
    errors = []
    export_task.get_buffered_stream(stream_type=lb.StreamType.ERRORS).start(
        stream_handler=lambda output: errors.append(output.json)
    )
    logger.error(f"Export errors: {errors}")
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

if args.workflow_step:
    queues = lb_project.task_queues()
    queue = next((q for q in queues if q.name == args.workflow_step), None)
    if not queue:
        available = [q.name for q in queues]
        logger.error(f"Task queue '{args.workflow_step}' not found. Available: {available}")
        sys.exit(1)
    lb_project.move_data_rows_to_task_queue(lb.UniqueIds(data_row_ids), task_queue_id=queue.uid)
    logger.info(f"Data rows moved to task queue '{args.workflow_step}'.")

if args.model_run_id:
    import_job = lb.MEAToMALPredictionImport.create_for_model_run_data_rows(
        client=client,
        model_run_id=args.model_run_id,
        data_row_ids=data_row_ids,
        project_id=lb_project.uid,
        name=mission_id,
    )
    import_job.wait_until_done()
    if import_job.errors:
        logger.error(f"Prediction import errors: {import_job.errors}")
        sys.exit(1)
    logger.info(f"Model predictions imported from run '{args.model_run_id}'.")
