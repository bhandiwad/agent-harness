"""Trusted application worker: explicitly register the sample's completion check."""

import asyncio
import os
import sys
from pathlib import Path

from checks import check_plan
from pandita_agent.agent.contracts import load_settings
from pandita_agent.agent.main import work
from pandita_agent.agent.provider import Provider
from pandita_agent.agent.runner import Runner
from pandita_agent.agent.storage import Store, check_role, database


def main():
    settings = load_settings(Path(sys.argv[1]))
    engine = database(settings, os.environ)
    try:
        check_role(engine)
        runner = Runner(
            Store(engine, settings),
            Provider(os.environ),
            completion_checks={"maintenance-v1": check_plan},
        )
        asyncio.run(work(runner))
    finally:
        engine.dispose()


if __name__ == "__main__":
    main()
