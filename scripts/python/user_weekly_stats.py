"""
Weekly per-user activity stats from a Labelbox NDJSON export.

For a given user email and date range, counts per week:
  - labeled:  labels the user created (label_details.created_by / created_at)
  - reviewed: review actions the user performed (Approve + Reject in workflow_history)
  - approved: Approve actions (accepted / done)
  - rejected: Reject actions (sent to rework)

Also counts issues the user created, per week and per category: FLOR, FRUTO
and "FLOR y FRUTO" are counted together, every other category is reported
separately, uncategorized issues are excluded. Issues are not part of the
export file, so they are fetched live from the Labelbox API (requires
LABELBOX_API_KEY in .env) for every project id found in the export.

Usage (run from repo root):
    python scripts/python/user_weekly_stats.py \
        --export /data/sharing/labelbox/... \
        --user user@domain.com \
        --start 2026-01-01 --end 2026-06-30
"""

import argparse
import json
import urllib.request

from collections import defaultdict
from datetime import date, datetime, timedelta

from _common import get_client

METRICS = ["labeled", "reviewed", "approved", "rejected"]

FLOR_FRUTO = {"FLOR", "FRUTO", "FLOR y FRUTO"}


def week_start(d: date) -> date:
    """Monday of the week containing d."""
    return d - timedelta(days=d.weekday())


def parse_ts(ts: str) -> date:
    return datetime.fromisoformat(ts).date()


def main() -> None:
    parser = argparse.ArgumentParser(description="Weekly per-user stats from a Labelbox NDJSON export.")
    parser.add_argument("--export", required=True, help="Path to the NDJSON export file.")
    parser.add_argument("--user", required=True, help="User email as it appears in created_by fields.")
    parser.add_argument("--start", required=True, help="Start date (YYYY-MM-DD), inclusive.")
    parser.add_argument("--end", required=True, help="End date (YYYY-MM-DD), inclusive.")
    args = parser.parse_args()

    start = date.fromisoformat(args.start)
    end = date.fromisoformat(args.end)

    # weeks[monday][metric] = count
    weeks: dict[date, dict[str, int]] = defaultdict(lambda: dict.fromkeys(METRICS, 0))

    def add(d: date, metric: str) -> None:
        if start <= d <= end:
            weeks[week_start(d)][metric] += 1

    project_ids: set[str] = set()

    with open(args.export) as f:
        for line in f:
            row = json.loads(line)
            project_ids.update(row.get("projects", {}))
            for project in row.get("projects", {}).values():
                for label in project.get("labels", []):
                    details = label.get("label_details", {})
                    if details.get("created_by") == args.user:
                        add(parse_ts(details["created_at"]), "labeled")
                for event in project.get("project_details", {}).get("workflow_history", []):
                    if event.get("created_by") != args.user:
                        continue
                    action = event.get("action")
                    if action in ("Approve", "Reject"):
                        d = parse_ts(event["created_at"])
                        add(d, "reviewed")
                        add(d, "approved" if action == "Approve" else "rejected")

    # issues[monday][category] = count. FLOR / FRUTO / "FLOR y FRUTO" are
    # merged into one category; uncategorized issues are skipped.
    issues: dict[date, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    client = get_client()
    for project_id in sorted(project_ids):
        url = client.get_project(project_id).export_issues()
        for issue in json.load(urllib.request.urlopen(url)):
            if issue.get("createdBy") != args.user:
                continue
            category = issue.get("categoryName")
            if category is None:
                continue
            if category in FLOR_FRUTO:
                category = "FLOR/FRUTO"
            d = parse_ts(issue["createdAt"].replace("Z", "+00:00"))
            if start <= d <= end:
                issues[week_start(d)][category] += 1

    print(f"Stats for {args.user} from {start} to {end} (weeks start on Monday)\n")
    header = f"{'week of':<12}" + "".join(f"{m:>10}" for m in METRICS)
    print(header)
    print("-" * len(header))
    totals = dict.fromkeys(METRICS, 0)
    for monday in sorted(weeks):
        counts = weeks[monday]
        print(f"{monday.isoformat():<12}" + "".join(f"{counts[m]:>10}" for m in METRICS))
        for m in METRICS:
            totals[m] += counts[m]
    print("-" * len(header))
    print(f"{'TOTAL':<12}" + "".join(f"{totals[m]:>10}" for m in METRICS))

    print(f"\nIssues created by {args.user} (rows: category, columns: week of)\n")
    mondays = sorted(issues)
    category_totals = defaultdict(int)
    for counts in issues.values():
        for category, n in counts.items():
            category_totals[category] += n
    name_width = max((len(c) for c in category_totals), default=8) + 2
    header = f"{'category':<{name_width}}" + "".join(f"{m.strftime('%m-%d'):>7}" for m in mondays) + f"{'TOTAL':>8}"
    print(header)
    print("-" * len(header))
    for category in sorted(category_totals, key=category_totals.get, reverse=True):
        cells = "".join(f"{issues[m].get(category, 0):>7}" for m in mondays)
        print(f"{category:<{name_width}}{cells}{category_totals[category]:>8}")
    print("-" * len(header))
    week_cells = "".join(f"{sum(issues[m].values()):>7}" for m in mondays)
    print(f"{'TOTAL':<{name_width}}{week_cells}{sum(category_totals.values()):>8}")


if __name__ == "__main__":
    main()
