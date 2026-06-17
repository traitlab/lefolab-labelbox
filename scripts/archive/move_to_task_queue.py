"""
Move data rows from one or more projects to a specific task queue in Labelbox.

Reads data row IDs from a CSV and calls the Labelbox API to move them to
the specified task queue, iterating over each project in PROJECT_NAMES.

Usage:
    Set the variables below, then run:
        python move_to_task_queue.py
"""

import os
import pandas as pd
import labelbox as lb
from dotenv import load_dotenv

load_dotenv()

# ── Configuration ──────────────────────────────────────────────────────────────
CSV_PATH      = r"<path/to/your/labelbox_export.csv>"
PROJECT_NAMES = ["<project_name_1>", "<project_name_2>"]  # one or more project names
TASK_QUEUE_ID = "<task_queue_id>"        # target task queue UUID
BATCH_SIZE    = 1000                     # Labelbox recommends batching large moves

COL_PROJECT_NAME = "project_name"   # column used to filter by project
COL_DATA_ROW_ID  = "lb_data_row_id" # column containing Labelbox data row IDs
COL_PROJECT_ID   = "project_id"     # column containing Labelbox project IDs

# ── Labelbox client ────────────────────────────────────────────────────────────
api_key = os.environ.get("LABELBOX_API_KEY")
if not api_key:
    raise EnvironmentError("LABELBOX_API_KEY not set. Add it to your .env file.")

client = lb.Client(api_key=api_key)

# ── Load data ──────────────────────────────────────────────────────────────────
df = pd.read_csv(CSV_PATH)

total_moved = 0

for project_name in PROJECT_NAMES:
    print(f"\n── Project: {project_name} ──")

    mask = df[COL_PROJECT_NAME] == project_name
    subset = df.loc[mask, [COL_DATA_ROW_ID, COL_PROJECT_ID]].drop_duplicates()

    data_row_ids = subset[COL_DATA_ROW_ID].dropna().tolist()
    project_ids  = subset[COL_PROJECT_ID].unique().tolist()

    print(f"  Project ID(s) found : {project_ids}")
    print(f"  Data rows to move   : {len(data_row_ids)}")

    if not data_row_ids:
        print("  No data rows found — skipping.")
        continue

    assert len(project_ids) == 1, f"Expected 1 project id for '{project_name}', got: {project_ids}"
    project_id = project_ids[0]

    project = client.get_project(project_id)
    print(f"  Labelbox project    : {project.name}  (id: {project_id})")

    # ── Move data rows in batches ──────────────────────────────────────────────
    for i in range(0, len(data_row_ids), BATCH_SIZE):
        batch = data_row_ids[i : i + BATCH_SIZE]
        print(f"  Moving rows {i+1}–{i+len(batch)} …", end=" ", flush=True)
        project.move_data_rows_to_task_queue(
            data_row_ids=lb.UniqueIds(batch),
            task_queue_id=TASK_QUEUE_ID,
        )
        print("done")

    total_moved += len(data_row_ids)

print(f"\nDone. {total_moved} data rows moved to task queue {TASK_QUEUE_ID} across {len(PROJECT_NAMES)} project(s).")