"""Small native tools. Workspace files are scoped database artifacts, never host paths."""

import re
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation, localcontext
from pathlib import PurePosixPath


class WorkspaceConflict(ValueError):
    """A local edit was rejected before mutation; the model may read and correct it."""


class ToolInputError(ValueError):
    """Safe, actionable input error raised before a local mutation commits."""


def schema(properties, required):
    return {
        "type": "object",
        "properties": properties,
        "required": required,
        "additionalProperties": False,
    }


PATH = {"type": "string", "minLength": 1, "maxLength": 160, "pattern": "^[a-zA-Z0-9_./-]+$"}
NATIVE = {
    "harness_time": {
        "description": "Get the current UTC time.",
        "schema": schema({}, []),
        "permission": None,
    },
    "harness_calculate": {
        "description": "Perform bounded decimal arithmetic without executing code. Unit is an explicit label, not a conversion.",
        "schema": schema(
            {
                "operation": {"type": "string", "enum": ["add", "subtract", "multiply", "divide"]},
                "left": {"type": "string", "maxLength": 64},
                "right": {"type": "string", "maxLength": 64},
                "unit": {"type": "string", "maxLength": 64},
            },
            ["operation", "left", "right", "unit"],
        ),
        "permission": None,
    },
    "harness_list_files": {
        "description": "List files in this conversation's private workspace.",
        "schema": schema({}, []),
        "permission": "workspace:read",
    },
    "harness_read_file": {
        "description": "Read a UTF-8 workspace file with one-based line numbers and its exact digest.",
        "schema": schema(
            {
                "path": PATH,
                "start_line": {"type": "integer", "minimum": 1},
                "max_lines": {"type": "integer", "minimum": 1, "maximum": 200},
            },
            ["path", "start_line", "max_lines"],
        ),
        "permission": "workspace:read",
    },
    "harness_search_files": {
        "description": "Search literal text in this conversation's files. No regular expressions or host filesystem access.",
        "schema": schema(
            {"query": {"type": "string", "minLength": 1, "maxLength": 256}}, ["query"]
        ),
        "permission": "workspace:read",
    },
    "harness_write_file": {
        "description": "Create or update a private UTF-8 artifact in this conversation. Supply the current digest to replace an existing file; null creates a new file. Does not publish or send it.",
        "schema": schema(
            {
                "path": PATH,
                "content": {"type": "string", "maxLength": 32768},
                "expected_digest": {"type": ["string", "null"], "pattern": "^[a-f0-9]{64}$"},
            },
            ["path", "content", "expected_digest"],
        ),
        "permission": "workspace:write",
    },
}


def valid_path(value):
    path = PurePosixPath(value)
    if (
        not re.fullmatch(r"[a-zA-Z0-9_./-]{1,160}", value)
        or path.is_absolute()
        or ".." in path.parts
        or str(path) != value
        or value in {".", ""}
    ):
        raise ValueError("Use a normalized relative workspace path.")
    return value


def write_file(state, path, content, expected_digest):
    import hashlib

    valid_path(path)
    raw = content.encode("utf-8")
    if len(raw) > 32768 or "\x00" in content:
        raise ToolInputError("Workspace files must be bounded UTF-8 text, without null bytes.")
    files = state.setdefault("files", {})
    previous = files.get(path)
    if (previous or {}).get("digest") != expected_digest:
        raise WorkspaceConflict("Workspace file changed or already exists.")
    if len(files) >= 32 and previous is None:
        raise ValueError("Workspace file count reached its limit.")
    value = {
        "content": content,
        "digest": hashlib.sha256(raw).hexdigest(),
        "bytes": len(raw),
        "version": (previous or {}).get("version", 0) + 1,
    }
    files[path] = value
    return {
        "path": path,
        **{key: value[key] for key in ("digest", "bytes", "version")},
        "published": False,
    }


def invoke(name, arguments, state):
    files = state.get("files", {})
    if name == "harness_time":
        return {"utc": datetime.now(UTC).isoformat()}
    if name == "harness_calculate":
        try:
            left, right = Decimal(arguments["left"]), Decimal(arguments["right"])
            if any(not value.is_finite() or abs(value.adjusted()) > 30 for value in (left, right)):
                raise ToolInputError("Use finite decimals with exponents between -30 and 30.")
            with localcontext() as ctx:
                ctx.prec = 28
                operations = {
                    "add": lambda: left + right,
                    "subtract": lambda: left - right,
                    "multiply": lambda: left * right,
                    "divide": lambda: left / right,
                }
                value = operations[arguments["operation"]]()
            if not value.is_finite() or abs(value.adjusted()) > 60:
                raise ToolInputError("Result exceeds arithmetic bounds; use smaller operands.")
            return {"value": str(value), "unit": arguments["unit"], "decimal_precision": 28}
        except (InvalidOperation, ArithmeticError) as exc:
            raise ToolInputError("Use valid finite decimals and a nonzero divisor.") from exc
    if name == "harness_list_files":
        return {
            "files": [
                {"path": path, **{key: value[key] for key in ("digest", "bytes", "version")}}
                for path, value in sorted(files.items())
            ]
        }
    if name == "harness_write_file":
        return write_file(
            state, arguments["path"], arguments["content"], arguments["expected_digest"]
        )
    if name == "harness_read_file":
        path = valid_path(arguments["path"])
        file = files.get(path)
        if file is None:
            raise ToolInputError(
                "Workspace file not found. Use harness_list_files for available paths."
            )
        lines = file["content"].splitlines()
        start = arguments["start_line"] - 1
        selected = lines[start : start + arguments["max_lines"]]
        return {
            "path": path,
            "digest": file["digest"],
            "version": file["version"],
            "total_lines": len(lines),
            "lines": [
                {"line": start + index + 1, "text": value} for index, value in enumerate(selected)
            ],
            "has_more": start + len(selected) < len(lines),
        }
    if name == "harness_search_files":
        matches = [
            {"path": path, "digest": file["digest"], "line": index + 1, "text": line}
            for path, file in sorted(files.items())
            for index, line in enumerate(file["content"].splitlines())
            if arguments["query"].casefold() in line.casefold()
        ]
        return {"matches": matches[:100], "truncated": len(matches) > 100}
    raise ValueError("Native tool not installed.")
