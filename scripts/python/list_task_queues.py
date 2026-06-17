"""
List all task queues of a Labelbox project.

Useful for finding the exact queue names / ids to pass to move_datarows.py
(--source_task / --target_task).

Usage (run from repo root):
    python scripts/python/list_task_queues.py --project 2024_bci
"""

import argparse
import labelbox as lb
import os
import sys

from dotenv import load_dotenv
from pathlib import Path

project_root = Path(__file__).parent.parent.parent
load_dotenv(dotenv_path=project_root / ".env")


def main() -> None:
    parser = argparse.ArgumentParser(description="List the task queues of a Labelbox project.")
    parser.add_argument("--project", required=True, help="Labelbox project name.")
    args = parser.parse_args()

    api_key = os.getenv("LABELBOX_API_KEY")
    if not api_key:
        sys.exit("LABELBOX_API_KEY environment variable is not set")

    client = lb.Client(api_key=api_key)

    projects = [p for p in client.get_projects() if p.name == args.project]
    if not projects:
        sys.exit(f"Labelbox project '{args.project}' not found.")
    if len(projects) > 1:
        sys.exit(f"Multiple projects named '{args.project}' found. Cannot disambiguate.")
    project = projects[0]

    print(f"Task queues for project '{project.name}' ({project.uid}):\n")
    for q in project.task_queues():
        print(f"  {q.name}  —  {q.data_row_count} data rows")
        print(f"    id: {q.uid}   type: {q.queue_type}")


if __name__ == "__main__":
    main()
