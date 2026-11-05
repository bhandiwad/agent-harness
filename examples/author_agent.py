"""Save a reviewed agent revision; run from the exported package's directory."""

import argparse
from pathlib import Path

from pandita_agent.agent.sdk import load_settings, pin_instructions, pin_skill, write_config


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", type=Path, default=Path("config/agent.json"))
    parser.add_argument("--project", type=Path, required=True)
    parser.add_argument("--skill", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    settings = load_settings(args.config)
    agent = settings.agents[0].model_copy(
        update={
            "instruction_tree": pin_instructions(args.project),
            "skills": (pin_skill(args.skill, version="v1"),),
            "skill_mode": "on_demand",
        }
    )
    write_config(settings.model_copy(update={"agents": (agent, *settings.agents[1:])}), args.output)
    print("Saved a new configuration for review; it has not been installed.")


if __name__ == "__main__":
    main()
