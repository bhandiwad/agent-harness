import argparse
import asyncio
import logging
import os
import time
from pathlib import Path

import uvicorn

from pandita_agent.agent.api import create_app
from pandita_agent.agent.contracts import load_settings
from pandita_agent.agent.provider import Provider
from pandita_agent.agent.runner import Runner
from pandita_agent.agent.storage import Store, check_role, database, migrate


async def work(runner, concurrency=4, maintenance=None):
    # Each running session holds one writer connection; keep headroom in the
    # eight-connection pool for record transactions and polling.
    limit = min(max(concurrency, 1), 4, runner.settings.max_active_sessions)
    tasks = {}

    async def tick(ident):
        try:
            await runner.tick(ident)
        except Exception:
            # Back off failed sessions without blocking unrelated work or tight retry loops.
            await asyncio.sleep(0.5)
            raise

    last_maintenance = 0.0
    try:
        while True:
            if maintenance and time.monotonic() - last_maintenance > 2:
                try:
                    await asyncio.to_thread(maintenance, runner)
                except Exception:
                    logging.getLogger(__name__).error(
                        "Runtime maintenance unavailable; pending work retained."
                    )
                last_maintenance = time.monotonic()
            for ident, task in list(tasks.items()):
                if task.done():
                    del tasks[ident]
                    try:
                        task.result()
                    except Exception:
                        # Do not expose exception text, credentials or user content.
                        logging.getLogger(__name__).error(
                            "Session processing failed; durable work retained."
                        )
            try:
                pending = await asyncio.to_thread(runner.store.list, pending=True)
            except Exception:
                logging.getLogger(__name__).error(
                    "Session storage unavailable; retrying polling."
                )
                await asyncio.sleep(1)
                continue
            for row in pending:
                if len(tasks) >= limit:
                    break
                if row["id"] not in tasks:
                    tasks[row["id"]] = asyncio.create_task(tick(row["id"]))
            if tasks:
                await asyncio.wait(
                    tasks.values(), timeout=0.5, return_when=asyncio.FIRST_COMPLETED
                )
            else:
                await asyncio.sleep(0.5)
    finally:
        for task in tasks.values():
            task.cancel()
        await asyncio.gather(*tasks.values(), return_exceptions=True)


def main():
    parser = argparse.ArgumentParser(
        description="Generic agent service; no report workflow required"
    )
    parser.add_argument("command", choices=("migrate", "api", "worker"))
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--port", type=int, default=8093)
    parser.add_argument("--host", default="127.0.0.1", choices=("127.0.0.1", "0.0.0.0"))
    args = parser.parse_args()
    for name in ("httpx2", "sqlalchemy.engine"):
        logging.getLogger(name).setLevel(logging.CRITICAL)
    try:
        settings = load_settings(args.config)
        engine = database(settings, os.environ)
        if args.command == "migrate":
            migrate(engine)
        else:
            check_role(engine)
            runner = Runner(Store(engine, settings), Provider(os.environ))
            if args.command == "api":
                uvicorn.run(
                    create_app(runner), host=args.host, port=args.port, access_log=False
                )
            else:
                asyncio.run(work(runner))
        engine.dispose()
        return 0
    except KeyboardInterrupt:
        return 0
    except Exception:
        print(
            "Agent service stopped; check installation, permissions and dependency health."
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
