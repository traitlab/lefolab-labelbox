"""
Move data rows to a task queue in a Labelbox project, with optional filters.

Data rows are selected by live-exporting the project and keeping only the rows
that match the filters supplied on the command line:

    --source_task       current task queue (name or id) the row sits in
    --labeler           email of the labeler who created a label on the row
    --gbif_id           taxon GBIF id (numeric species answer value) on the row

Filters combine as AND across types, OR within a type. For example,
`--source_task "Initial review task" --labeler a@x.com b@y.com` keeps rows that
are in the review task AND were labeled by a@x.com OR b@y.com.

At least one filter is required (moving an entire project unfiltered is not
allowed). The matching rows and their count are printed for review, then the
move runs only after you type 'yes'.

Usage (run from repo root):
    python scripts/python/move_datarows.py --project 2024_BCI \
        --target_task "Initial review task" \
        --source_task "Initial labeling task" \
        --labeler john.doe@si.edu \
        --gbif_id 12345 6789
"""

import argparse
import labelbox as lb
import sys

from _common import get_client, resolve_project, setup_logging

logger = setup_logging()


def collect_species_values(annotations: dict) -> set:
    """Collect numeric species values (GBIF ids) from a label's annotations.

    Only radio/checklist answer values are considered — these hold the taxon
    answer (e.g. a species' GBIF id). Values are kept only when numeric, which
    selects taxon answers and skips non-numeric answers (e.g. organ codes) as
    well as tool and classification values.
    """
    values = set()

    def walk_classifications(classifications):
        for c in classifications or []:
            radio = c.get("radio_answer")
            if radio:
                if (radio.get("value") or "").isdigit():
                    values.add(radio["value"])
                walk_classifications(radio.get("classifications"))
            for ans in c.get("checklist_answers") or []:
                if (ans.get("value") or "").isdigit():
                    values.add(ans["value"])
                walk_classifications(ans.get("classifications"))

    for obj in annotations.get("objects") or []:
        walk_classifications(obj.get("classifications"))
    walk_classifications(annotations.get("classifications"))
    return values


def resolve_target_queue(project, token: str):
    """Resolve a task queue token (name or uid) to a TaskQueue on the project."""
    queues = project.task_queues()
    match = next((q for q in queues if q.uid == token or q.name == token), None)
    if match is None:
        available = ", ".join(f"'{q.name}'" for q in queues)
        logger.error(f"Target task queue '{token}' not found. Available queues: {available}")
        sys.exit(1)
    return match


def main() -> None:
    parser = argparse.ArgumentParser(description="Move filtered data rows to a task queue in Labelbox.")
    parser.add_argument("--project", required=True, help="Labelbox project name.")
    parser.add_argument("--target_task", required=True, help="Target task queue (name or id) to move data rows INTO.")
    parser.add_argument("--source_task", nargs="+", help="Filter: only rows currently in these task queue(s) (name or id).")
    parser.add_argument("--labeler", nargs="+", help="Filter: only rows with a label created by these labeler email(s).")
    parser.add_argument("--gbif_id", nargs="+", help="Filter: only rows whose annotations include a taxon answer with this GBIF id (numeric value).")
    parser.add_argument("--batch_size", type=int, default=1000, help="Batch size for the move (default 1000).")
    args = parser.parse_args()

    source_tasks = set(args.source_task or [])
    labelers = set(args.labeler or [])
    gbif_ids = set(args.gbif_id or [])

    if not (source_tasks or labelers or gbif_ids):
        logger.error("At least one filter is required: --source_task, --labeler, and/or --gbif_id.")
        sys.exit(1)

    client = get_client()

    project = resolve_project(client, args.project)
    logger.info(f"Project: {project.name} (id: {project.uid})")

    target_queue = resolve_target_queue(project, args.target_task)
    logger.info(f"Target task queue: '{target_queue.name}' (id: {target_queue.uid})")

    # Export the project to get task queue, labeler, and annotation info per row
    logger.info("Exporting project from Labelbox…")
    export_task = project.export(params={"project_details": True, "label_details": True})
    export_task.wait_till_done()
    if export_task.has_errors():
        logger.warning("Export task reported errors; continuing with the rows that exported successfully.")
    if not export_task.has_result():
        logger.error("Export task produced no results.")
        sys.exit(1)

    matched = []
    for row in export_task.get_buffered_stream(stream_type=lb.StreamType.RESULT):
        rec = row.json
        pdata = rec.get("projects", {}).get(project.uid)
        if not pdata:
            continue

        details = pdata.get("project_details", {})
        task_id = details.get("task_id")
        task_name = details.get("task_name")

        labels = pdata.get("labels", [])
        row_labelers = {l.get("label_details", {}).get("created_by") for l in labels}
        row_labelers.discard(None)
        row_gbif_ids = set()
        for l in labels:
            row_gbif_ids |= collect_species_values(l.get("annotations", {}))

        task_ok = not source_tasks or task_id in source_tasks or task_name in source_tasks
        labeler_ok = not labelers or bool(row_labelers & labelers)
        gbif_ok = not gbif_ids or bool(row_gbif_ids & gbif_ids)

        if task_ok and labeler_ok and gbif_ok:
            matched.append({
                "data_row_id": rec["data_row"]["id"],
                "global_key": rec["data_row"].get("global_key") or "",
                "task_name": task_name or "",
                "task_id": task_id or "",
                "labelers": ", ".join(sorted(row_labelers & labelers)) if labelers else "",
                "gbif_ids": ", ".join(sorted(row_gbif_ids & gbif_ids)) if gbif_ids else "",
            })

    if not matched:
        logger.info("No data rows match the given filters. Nothing to move.")
        return

    # Show what will be moved
    logger.info(f"{len(matched)} data row(s) match the filters and will be moved to '{target_queue.name}':")
    for m in matched:
        extra = []
        if m["task_name"] or m["task_id"]:
            extra.append(f"task='{m['task_name']}' (id={m['task_id']})")
        if m["labelers"]:
            extra.append(f"labeler={m['labelers']}")
        if m["gbif_ids"]:
            extra.append(f"gbif_id={m['gbif_ids']}")
        logger.info(f"  {m['data_row_id']}  {m['global_key']}  [{'; '.join(extra)}]")

    try:
        answer = input(
            f"\nMove {len(matched)} data row(s) to task queue '{target_queue.name}' ({target_queue.uid})? "
            "Type 'yes' to proceed: "
        ).strip().lower()
    except (EOFError, KeyboardInterrupt):
        print()
        logger.info("Aborted.")
        return

    if answer != "yes":
        logger.info("Aborted.")
        return

    data_row_ids = [m["data_row_id"] for m in matched]
    for i in range(0, len(data_row_ids), args.batch_size):
        batch = data_row_ids[i:i + args.batch_size]
        logger.info(f"Moving rows {i + 1}–{i + len(batch)} of {len(data_row_ids)}…")
        project.move_data_rows_to_task_queue(
            data_row_ids=lb.UniqueIds(batch),
            task_queue_id=target_queue.uid,
        )

    logger.info(f"Done. {len(data_row_ids)} data row(s) moved to task queue '{target_queue.name}'.")


if __name__ == "__main__":
    main()
