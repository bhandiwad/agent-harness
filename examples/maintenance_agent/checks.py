"""Sample-specific, deterministic artifact checks; no model judges or extra calls."""

import csv
import io
import json
import math

from pandita_agent.agent.sdk import CompletionResult


def check_plan(context):
    files = context.files
    missing = [name for name in ("plan.md", "schedule.json", "assets.csv") if name not in files]
    if missing:
        return CompletionResult(
            issues=("Required workspace files are missing: " + ", ".join(missing),)
        )
    if not files["plan.md"]["content"].strip():
        return CompletionResult(issues=("Write the maintenance plan in plan.md.",))
    try:
        plan = json.loads(files["schedule.json"]["content"])
    except (ValueError, TypeError):
        return CompletionResult(issues=("schedule.json must contain a JSON object.",))
    numbers = (
        "technicians",
        "inspection_minutes_per_pump",
        "setup_minutes",
        "window_minutes",
        "total_inspection_person_minutes",
        "required_elapsed_minutes",
    )
    fields = set(numbers) | {"included_pumps", "excluded_pumps", "fits_window", "source"}
    if not isinstance(plan, dict) or set(plan) != fields:
        return CompletionResult(
            issues=(
                "Use exactly the schedule.json fields specified by the maintenance-planning skill.",
            )
        )
    if any(type(plan[k]) is not int or plan[k] < 0 or plan[k] > 1000000 for k in numbers):
        return CompletionResult(
            issues=(
                "Schedule quantities must be non-negative bounded integers, not strings or booleans.",
            )
        )
    if any(plan[k] == 0 for k in ("technicians", "inspection_minutes_per_pump", "window_minutes")):
        return CompletionResult(
            issues=("Crew size, inspection duration and maintenance window must be positive.",)
        )
    ids = {row["pump_id"] for row in csv.DictReader(io.StringIO(files["assets.csv"]["content"]))}
    groups = [plan["included_pumps"], plan["excluded_pumps"]]
    if any(
        not isinstance(group, list) or any(not isinstance(v, str) for v in group)
        for group in groups
    ):
        return CompletionResult(issues=("Included and excluded pumps must be lists of pump IDs.",))
    included, excluded = map(set, groups)
    issues = []
    if (
        any(len(set(group)) != len(group) for group in groups)
        or included & excluded
        or included | excluded != ids
    ):
        issues.append(
            "Account for every source pump exactly once, either included or excluded, without invented IDs."
        )
    expected_person = len(included) * plan["inspection_minutes_per_pump"]
    expected_elapsed = (
        plan["setup_minutes"]
        + math.ceil(len(included) / plan["technicians"]) * plan["inspection_minutes_per_pump"]
    )
    if plan["total_inspection_person_minutes"] != expected_person:
        issues.append(
            f"total_inspection_person_minutes must equal {expected_person} for the selected pumps and inspection duration."
        )
    if plan["required_elapsed_minutes"] != expected_elapsed:
        issues.append(
            f"required_elapsed_minutes must equal {expected_elapsed}: setup plus whole inspection rounds per crew."
        )
    if type(plan["fits_window"]) is not bool or plan["fits_window"] != (
        expected_elapsed <= plan["window_minutes"]
    ):
        issues.append(
            "fits_window must reflect whether the correctly calculated elapsed time fits the window. Do not shorten scope to make it fit."
        )
    if (
        not isinstance(plan["source"], str)
        or "assets.csv" not in plan["source"]
        or "planning-rules.md" not in plan["source"]
    ):
        issues.append("Name assets.csv and references/planning-rules.md in source.")
    return CompletionResult(issues=tuple(issues))
