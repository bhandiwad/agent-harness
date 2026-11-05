"""The app validator checks independently computed constraints, not model wording."""

import importlib.util
import json
from pathlib import Path

import pytest
from pandita_agent.agent.sdk import CompletionContext

spec = importlib.util.spec_from_file_location(
    "maintenance_checks", Path(__file__).with_name("checks.py")
)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


def context(**changes):
    schedule = {
        "included_pumps": ["P01", "P02", "P03", "P04", "P05", "P06"],
        "excluded_pumps": ["P07", "P08"],
        "technicians": 2,
        "inspection_minutes_per_pump": 45,
        "setup_minutes": 30,
        "window_minutes": 120,
        "total_inspection_person_minutes": 270,
        "required_elapsed_minutes": 165,
        "fits_window": False,
        "source": "assets.csv and references/planning-rules.md",
        **changes,
    }
    files = {
        "plan.md": {"content": "Six pumps need 165 minutes and exceed the window."},
        "schedule.json": {"content": json.dumps(schedule)},
        "assets.csv": {
            "content": Path(__file__).with_name("data").joinpath("assets.csv").read_text()
        },
    }
    return CompletionContext(
        answer="Saved", requests=("Keep P07 and P08 excluded",), facts={}, files=files
    )


def test_valid_infeasible_plan_passes_without_silently_shortening_scope():
    assert not module.check_plan(context()).issues
    assert not module.check_plan(context(window_minutes=180, fits_window=True)).issues


@pytest.mark.parametrize(
    "change",
    [
        {"required_elapsed_minutes": 135},
        {"total_inspection_person_minutes": 240},
        {"fits_window": True},
        {"technicians": True},
        {"technicians": 0},
        {"excluded_pumps": ["P06", "P08"]},
        {"included_pumps": ["P01", "P01"]},
        {"included_pumps": ["P01", "P02", "P03", "P04", "P05", "P99"]},
        {"source": "Model knowledge"},
    ],
)
def test_wrong_artifacts_are_rejected(change):
    assert module.check_plan(context(**change)).issues


@pytest.mark.parametrize("path", ["plan.md", "schedule.json"])
def test_missing_artifacts_cannot_pass(path):
    value = context()
    del value.files[path]
    assert module.check_plan(value).issues
