"""The sample agent definition: edit this file and its skill to create your own agent."""

from pathlib import Path

from pandita_agent.agent.sdk import Agent, Capacity, pin_instructions, pin_skill

ROOT = Path(__file__).resolve().parent


def make_agent():
    return Agent(
        id="maintenance",
        model="demo",
        instructions="Help the user plan maintenance. Select the maintenance-planning skill for these tasks and follow its rules. Use tools one at a time. Preserve user constraints and exclusions across turns. Ask missing information using harness_ask_user. Save the requested artifacts and state feasibility clearly.",
        native_tools=(
            "harness_calculate",
            "harness_list_files",
            "harness_read_file",
            "harness_write_file",
        ),
        skills=(
            pin_skill(
                ROOT / "skills" / "maintenance-planning",
                required_tools=("harness_calculate", "harness_read_file", "harness_write_file"),
                permission="capability:maintenance",
            ),
        ),
        instruction_tree=pin_instructions(ROOT / "project"),
        capacity=Capacity(profile="bounded-demo", context_tokens=64000, output_tokens=2048),
        completion_check="maintenance-v1",
        max_steps=24,
        max_tokens=100000,
        max_seconds=240,
    )
