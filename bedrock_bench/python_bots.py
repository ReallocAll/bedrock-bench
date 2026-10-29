# SPDX-License-Identifier: Apache-2.0
"""Host-only wheel installation and fresh installed identity for the Python bot."""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import tempfile
from collections.abc import Callable, Mapping, Sequence
from datetime import datetime
from pathlib import Path
from typing import Any

from bedrock_bench.bots import (
    BotBinary,
    BotError,
    Target,
    ToolchainError,
    _manifest_path,
    _write_manifest,
    binary_path,
    sha256_file,
)
from bedrock_bench.config import ConfigError
from bedrock_bench.model import sha256_json

PACKAGE = "bedrock-bench-bot"
VERSION = "0.1.0.dev0"
WHEEL_NAME = f"bedrock_bench_bot-{VERSION}-py3-none-any.whl"
DEFAULT_PYTHON = "3.12"
NATIVE_PACKAGE = "endpilot"
NATIVE_VERSION = "0.1.0"


def acquire_server_identity(
    binary: BotBinary,
    *,
    pid: int,
    created: float,
    listener: Mapping[str, Any],
    timeout_s: float,
) -> dict[str, Any]:
    """Run only the verified installed Python helper, bounded by the caller's remaining boot deadline."""
    if binary.module != "python" or binary.argv_prefix != (str(binary.path), "-I", "-m", "bedrock_bench_bot"):
        raise BotError("managed-local server identity requires the installed Python bot")
    if timeout_s <= 0:
        raise BotError("managed-local server identity has no remaining boot time")
    command = [
        str(binary.path),
        "-I",
        "-m",
        "bedrock_bench_bot.server_identity",
        "--server-pid",
        str(pid),
        "--server-created",
        str(created),
        "--endpoint",
        listener["endpoint"],
        "--listener-address",
        listener["address"],
        "--listener-fd",
        str(listener["fd"]),
        "--timeout",
        str(min(10.0, timeout_s) * 0.9),
    ]
    try:
        result = subprocess.run(
            command,
            capture_output=True,
            timeout=min(10.0, timeout_s),
            check=False,
            stdin=subprocess.DEVNULL,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise BotError("managed-local server identity helper failed or exceeded the boot deadline") from exc
    try:
        if len(result.stdout) > 16384:
            raise ValueError
        info = json.loads(result.stdout)
        if not isinstance(info, dict):
            raise ValueError
    except ValueError:
        raise BotError("managed-local server identity helper returned invalid JSON") from None
    if result.returncode != 0:
        code = info.get("error")
        detail = code if isinstance(code, str) and re.fullmatch(r"[A-Za-z_]{1,80}", code) else "probe_failed"
        raise BotError(f"managed-local server identity failed: {detail}")
    try:
        timestamp = datetime.fromisoformat(info["verified_at"])
        if (
            info["verified"] is not True
            or type(info["pid"]) is not int
            or info["pid"] != pid
            or type(info["create_time"]) not in (int, float)
            or info["create_time"] != created
            or not isinstance(info["key_digest"], str)
            or re.fullmatch(r"[0-9a-f]{64}", info["key_digest"]) is None
            or info["endpoint"] != listener["endpoint"]
            or timestamp.tzinfo is None
            or timestamp.utcoffset().total_seconds() != 0
        ):
            raise ValueError
    except (KeyError, TypeError, ValueError, AttributeError):
        raise BotError("managed-local server identity result failed verification checks") from None
    return {key: info[key] for key in ("verified", "key_digest", "endpoint", "pid", "create_time", "verified_at")}


def _host_only(target: Target) -> None:
    if target != Target.host():
        raise BotError(f"the Python bot is host-only; cannot install or locate {target} on {Target.host()}")


def locate_python(*, target: Target, root: Path) -> BotBinary:
    """Always refresh package and dependency identity; an unchanged interpreter is insufficient."""
    from bedrock_bench.bots import version_info

    _host_only(target)
    path = binary_path("python", target, root)
    if not path.is_file():
        raise BotError(
            f"Python bot not found at {path}; run 'bbench bots build --module python' "
            "or 'bbench bots fetch --module python'"
        )
    prefix = (str(path), "-I", "-m", "bedrock_bench_bot")
    info = version_info(prefix)
    if (info.get("module"), info.get("protocol"), info.get("version")) != ("python", 2193, VERSION):
        raise BotError(
            f"Python bot must report module='python', protocol=2193 and version={VERSION}; "
            f"got {(info.get('module'), info.get('protocol'), info.get('version'))!r}"
        )
    try:
        environment = info["environment"]
        versions = {item["name"].lower().replace("_", "-"): item["version"] for item in environment["distributions"]}
    except (KeyError, TypeError, AttributeError):
        raise BotError(f"invalid Python environment identity at {path}") from None
    for name, expected in ((PACKAGE, VERSION), (NATIVE_PACKAGE, NATIVE_VERSION)):
        if versions.get(name) != expected:
            raise BotError(f"Python bot requires installed {name}=={expected}, got {versions.get(name)!r}")
    source = "installed"
    try:
        manifest = json.loads(_manifest_path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        manifest = None
    if isinstance(manifest, dict) and manifest.get("sha256") == sha256_json(info):
        source = str(manifest.get("source") or source)
    return BotBinary("python", path, sha256_json(info), target, info, source, prefix)


def install_python(
    action: str,
    *,
    target: Target,
    root: Path,
    python: str | None = None,
    find_links: Sequence[str] = (),
    no_index: bool = False,
    requirements: Sequence[Path] = (),
    wheel: Path | None = None,
    provenance: Mapping[str, Any] | None = None,
    run: Callable[..., Any] = subprocess.run,
) -> BotBinary:
    """Build a wheel or fetch the exact package, then install wheels into the managed host environment."""
    _host_only(target)
    requirement_files = []
    for value in requirements:
        requirement_path = Path(value).resolve()
        if not requirement_path.is_file():
            raise ConfigError(f"Python requirement file does not exist or is not a file: {requirement_path}")
        try:
            digest = sha256_file(requirement_path)
        except OSError as exc:
            raise ConfigError(f"cannot read Python requirement file {requirement_path}: {exc}") from exc
        requirement_files.append({"path": str(requirement_path), "sha256": digest})
    root = root.resolve()
    uv = os.environ.get("UV") or shutil.which("uv")
    if not uv:
        raise ToolchainError("uv is needed to build or install the Python bot")
    src = root / "bots" / "python"
    if action == "build" and not (src / "pyproject.toml").is_file():
        raise BotError(f"no Python adapter package at {src}")
    interpreter = python or DEFAULT_PYTHON
    path = binary_path("python", target, root)
    venv = path.parent.parent
    if not venv.resolve().is_relative_to(root):
        raise BotError(f"managed Python environment resolves outside the checkout: {venv}")
    venv.parent.mkdir(parents=True, exist_ok=True)
    local = root / ".local"
    local.mkdir(exist_ok=True)
    links = [part for location in find_links for part in ("--find-links", str(location))]
    indexes = ["--no-index"] if no_index else []
    requirement_args = [part for item in requirement_files for part in ("--requirement", item["path"])]
    env = dict(os.environ)
    for name in ("PYTHONHOME", "PYTHONPATH", "VIRTUAL_ENV"):
        env.pop(name, None)

    def invoke(command: list[str]) -> None:
        try:
            run(command, check=True, cwd=root, env=env)
        except subprocess.CalledProcessError as exc:
            raise ToolchainError(
                f"Python bot {action}: {' '.join(command[1:3])} failed with exit code {exc.returncode}"
            ) from exc
        except (OSError, subprocess.SubprocessError) as exc:
            raise ToolchainError(f"Python bot {action}: {' '.join(command[1:3])} failed: {exc}") from exc

    with tempfile.TemporaryDirectory(prefix="python-bot-", dir=local) as wheel_dir:
        requirement = str(wheel) if wheel is not None else f"{PACKAGE}=={VERSION}"
        if action == "build":
            invoke([uv, "build", "--no-config", "--wheel", "--python", interpreter, "--out-dir", wheel_dir, str(src)])
            wheels = list(Path(wheel_dir).glob(f"bedrock_bench_bot-{VERSION}-*.whl"))
            if len(wheels) != 1:
                raise BotError(f"Python adapter build did not produce exactly one {PACKAGE}=={VERSION} wheel")
            requirement = str(wheels[0])
        invoke([uv, "venv", "--no-config", "--no-project", "--clear", "--python", interpreter, str(venv)])
        invoke(
            [
                uv,
                "pip",
                "install",
                "--no-config",
                "--python",
                str(path),
                "--only-binary",
                ":all:",
                "--no-binary",
                "nethernet",
                *links,
                *indexes,
                *requirement_args,
                requirement,
                f"{NATIVE_PACKAGE}=={NATIVE_VERSION}",
            ]
        )
        invoke([uv, "pip", "check", "--no-config", "--python", str(path)])
    located = locate_python(target=target, root=root)
    binary = BotBinary("python", path, located.sha256, target, located.info, action, located.argv_prefix)
    _write_manifest(binary, {"requirements": requirement_files, **(provenance or {})})
    return binary
