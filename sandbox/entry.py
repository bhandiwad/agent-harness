"""Ephemeral container entry. Payloads and artifacts are untrusted data, never host paths."""

import json
import os
import re
import resource
import subprocess
import sys
from pathlib import Path, PurePosixPath


def path(value):
    if (
        not re.fullmatch(r"[a-zA-Z0-9_./-]{1,160}", value)
        or PurePosixPath(value).is_absolute()
        or ".." in PurePosixPath(value).parts
        or str(PurePosixPath(value)) != value
        or value == "."
        or value.startswith(".harness")
    ):
        raise ValueError("Invalid workspace path")
    return Path("/workspace") / value


def main():
    raw = sys.stdin.buffer.read(196609)
    if len(raw) > 196608:
        raise ValueError("Oversized job")
    payload = json.loads(raw)
    for name, content in payload["files"].items():
        destination = path(name)
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(content)
    request = payload["request"]
    env = {
        "PATH": "/usr/local/bin:/usr/bin:/bin",
        "HOME": "/tmp",
        "TMPDIR": "/tmp",
        "LANG": "C.UTF-8",
        "PYTHONDONTWRITEBYTECODE": "1",
        "PLAYWRIGHT_BROWSERS_PATH": "/opt/browsers",
    }
    kind = payload["kind"]
    if kind == "shell":
        command = ["/bin/sh", "-c", request["code"]]
    elif kind == "python":
        script = Path("/workspace/.harness-code.py")
        script.write_text(request["code"])
        command = [sys.executable, "-I", str(script)]
    elif kind == "browser":
        script = Path("/workspace/.harness-browser.json")
        script.write_text(json.dumps(request))
        command = [sys.executable, "-I", "/opt/executor/browser_job.py", str(script)]
    else:
        raise ValueError("Unsupported executor")
    if payload["require_landlock"]:
        command = [sys.executable, "-I", "/opt/executor/landlock.py", *command]
    resource.setrlimit(resource.RLIMIT_FSIZE, (1048576, 1048576))
    resource.setrlimit(resource.RLIMIT_NOFILE, (256, 256))
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    # Output files bound child I/O without retaining unbounded subprocess pipes in memory.
    with (
        open("/tmp/.harness-stdout", "w+b") as stdout,
        open("/tmp/.harness-stderr", "w+b") as stderr,
    ):
        child = subprocess.Popen(
            command,
            cwd="/workspace",
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=stdout,
            stderr=stderr,
            start_new_session=True,
        )
        try:
            code = child.wait(timeout=payload["max_seconds"])
        except subprocess.TimeoutExpired:
            os.killpg(child.pid, 9)
            child.wait()
            code = 124
        stdout.seek(0)
        out = stdout.read(16385)
        stderr.seek(0)
        err = stderr.read(8193)
    if len(out) > 16384 or len(err) > 8192:
        code = 122
        out = out[:16384]
        err = b"Output limit exceeded."
    files = {}
    if code == 0:
        for name in request["output_files"]:
            destination = path(name)
            # Resolve all components beneath the ephemeral workspace; deny links and devices.
            if any(
                parent.is_symlink()
                for parent in [destination, *destination.parents]
                if parent != Path("/")
            ):
                raise ValueError("Output symlinks are forbidden")
            fd = os.open(destination, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
            try:
                import stat

                if not stat.S_ISREG(os.fstat(fd).st_mode):
                    raise ValueError("Output is not a regular file")
                value = os.read(fd, 32769)
                if len(value) > 32768 or b"\0" in value:
                    raise ValueError("Output exceeds text artifact limits")
                files[name] = value.decode("utf-8")
            finally:
                os.close(fd)
        if sum(len(value.encode()) for value in files.values()) > 32768:
            raise ValueError("Combined outputs exceed artifact limit")
    print(
        json.dumps(
            {
                "exit_code": code,
                "stdout": out.decode("utf-8", errors="replace"),
                "stderr": err.decode("utf-8", errors="replace"),
                "files": files,
            }
        )
    )


if __name__ == "__main__":
    try:
        main()
    except Exception:
        print(
            json.dumps(
                {
                    "exit_code": 125,
                    "stdout": "",
                    "stderr": "Sandbox execution failed its bounds or artifact checks.",
                    "files": {},
                }
            )
        )
