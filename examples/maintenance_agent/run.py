"""Host launcher: python3 run.py --model-env /private/model.env [--chat | --ui]."""

import argparse
import json
import os
import shutil
import subprocess
import tempfile
from pathlib import Path
from uuid import uuid4


def main():
    parser = argparse.ArgumentParser(
        description="Run the sample against an isolated disposable database and a live model."
    )
    parser.add_argument("--model-env", type=Path, required=True)
    parser.add_argument("--output", type=Path)
    parser.add_argument(
        "--model-config",
        type=Path,
        help="Model JSON profile; overrides DEMO_MODEL and DEMO_MODEL_URL",
    )
    parser.add_argument("--ui-port", type=int, default=8094)
    parser.add_argument("--image", default="pandita-agent:0.4.0")
    parser.add_argument("--docker", default=shutil.which("docker"))
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--chat", action="store_true")
    mode.add_argument("--ui", action="store_true")
    args = parser.parse_args()
    if not 1024 <= args.ui_port <= 65535:
        parser.error("UI port must be between 1024 and 65535.")
    if not args.docker or not args.model_env.is_file():
        parser.error(
            "Supply an installed Docker CLI and an existing private model environment file."
        )
    if args.model_config and not args.model_config.is_file():
        parser.error("Model configuration file does not exist.")
    output = (
        args.output.resolve()
        if args.output
        else Path(tempfile.mkdtemp(prefix="pandita-agent-sample-")).resolve()
    )
    output.mkdir(parents=True, exist_ok=True)
    if any(output.iterdir()):
        parser.error("Use an empty output directory; prior results are preserved.")
    env = {
        key: os.environ[key]
        for key in ("PATH", "HOME", "DOCKER_HOST", "DOCKER_CONFIG")
        if key in os.environ
    }
    env.update(
        DEMO_OUTPUT_DIR=str(output),
        HARNESS_IMAGE=args.image,
        DEMO_UI_PORT=str(args.ui_port),
        DEMO_UID=str(os.getuid()),
        DEMO_GID=str(os.getgid()),
    )
    command = [
        args.docker,
        "compose",
        "--env-file",
        str(args.model_env.resolve()),
        "--project-name",
        "harness-sample-" + uuid4().hex[:10],
        "-f",
        str(Path(__file__).with_name("compose.yaml")),
    ]
    if args.model_config:
        override = output / "model-compose.json"
        override.write_text(
            json.dumps(
                {
                    "services": {
                        "demo": {
                            "environment": {"DEMO_MODEL_CONFIG": "/sample-model.json"},
                            "volumes": [
                                {
                                    "type": "bind",
                                    "source": str(args.model_config.resolve()),
                                    "target": "/sample-model.json",
                                    "read_only": True,
                                }
                            ],
                        }
                    }
                }
            )
        )
        command.extend(["-f", str(override)])
    if args.ui:
        override = output / "ui-compose.yaml"
        override.write_text(f'services:\n  db:\n    ports: ["127.0.0.1:{args.ui_port}:8094"]\n')
        command.extend(["-f", str(override)])
        print(
            f"Open http://localhost:{args.ui_port} once the UI is ready. Ctrl+C stops this sample.",
            flush=True,
        )
    print("Results: " + str(output), flush=True)
    try:
        subprocess.run([*command, "up", "-d", "--wait", "db"], env=env, check=True)
        run = [*command, "run", "--rm", "demo"] + (
            ["--chat"] if args.chat else ["--ui"] if args.ui else []
        )
        return subprocess.run(run, env=env, check=False).returncode
    finally:
        subprocess.run([*command, "down", "--volumes", "--remove-orphans"], env=env, check=False)


if __name__ == "__main__":
    raise SystemExit(main())
