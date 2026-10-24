# SPDX-License-Identifier: Apache-2.0
"""Identity of the actual installed interpreter and distributions, never a source checkout fallback."""

from __future__ import annotations

import asyncio
import csv
import hashlib
import importlib
import importlib.metadata as metadata
import json
import platform
import re
import sys
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

from . import __version__


def running_loop_info() -> dict:
    """Describe the loop actually executing the benchmark, independently of installed packages."""
    loop_type = type(asyncio.get_running_loop())
    return {
        "module": loop_type.__module__,
        "class": loop_type.__qualname__,
        "uvloop_version": metadata.version("uvloop") if loop_type.__module__.split(".")[0] == "uvloop" else None,
    }


def sha256(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def _direct_source(distribution) -> dict | None:
    raw = distribution.read_text("direct_url.json")
    if not raw:
        return None
    source = json.loads(raw)
    parsed = urlsplit(source.get("url", ""))
    # Credentials and query parameters never belong in provenance output.
    authority = parsed.hostname or ""
    if ":" in authority:
        authority = "[" + authority + "]"
    if parsed.port:
        authority += f":{parsed.port}"
    result = {"url": urlunsplit((parsed.scheme, authority, parsed.path, "", ""))}
    if "vcs_info" in source:
        result["vcs_info"] = {
            key: source["vcs_info"][key]
            for key in ("vcs", "commit_id", "requested_revision")
            if key in source["vcs_info"]
        }
    if "dir_info" in source:
        result["editable"] = bool(source["dir_info"].get("editable", False))
    if "archive_info" in source:
        archive = source["archive_info"]
        hashes = dict(archive.get("hashes", {}))
        # PEP610's older singular spelling is still emitted by installers for hash-pinned URLs.
        algorithm, separator, value = archive.get("hash", "").partition("=")
        if separator:
            hashes.setdefault(algorithm, value)
        # uv can retain a wheel's hash solely in the direct URL fragment. Preserve only a validated digest,
        # while arbitrary fragments and query strings remain redacted from provenance.
        digest = re.fullmatch(r"sha256=([a-fA-F0-9]{64})", parsed.fragment)
        if digest:
            hashes.setdefault("sha256", digest[1].lower())
        result["archive_hashes"] = hashes
    return result


def distribution_identity(distribution) -> dict:
    # Distribution.files omits missing entries on current Python versions. Use RECORD's inventory,
    # but hash the actual bytes: a stored digest must never conceal a changed or missing file.
    record = distribution.read_text("RECORD")
    if not record:
        raise RuntimeError("installed distribution has no file inventory: " + distribution.name)
    files = {}
    for relative in sorted(metadata.PackagePath(row[0]) for row in csv.reader(record.splitlines())):
        if "__pycache__" in relative.parts or relative.suffix == ".pyc":
            continue
        files[str(relative)] = sha256(Path(distribution.locate_file(relative)))
    return {
        "name": distribution.name,
        "version": distribution.version,
        "files": files,
        "direct_source": _direct_source(distribution),
    }


def build_info() -> dict:
    def executable(path):
        path = Path(path)
        return {"path": str(path), "sha256": sha256(path)}

    return {
        "module": "python",
        "version": __version__,
        "protocol": 2193,
        "patches": [],
        "environment": {
            "interpreter": {
                **executable(sys.executable),
                "version": platform.python_version(),
                "implementation": platform.python_implementation(),
                "base": executable(getattr(sys, "_base_executable", sys.executable)),
            },
            "distributions": sorted(
                (distribution_identity(item) for item in metadata.distributions()),
                key=lambda item: item["name"].lower(),
            ),
        },
    }


def load_sdk():
    """Require an installed SDK and ensure Python imported the installed distribution's file."""
    distribution = metadata.distribution("bedrock-bot")
    if distribution.version != "0.1.0":
        raise RuntimeError(f"bedrock-bot==0.1.0 is required; installed {distribution.version}")
    sdk = importlib.import_module("bedrock_bot")
    actual = Path(sdk.__file__).resolve()
    owned = {Path(distribution.locate_file(file)).resolve() for file in distribution.files or ()}
    if actual not in owned:
        raise RuntimeError(f"bedrock_bot imported outside its installed distribution: {actual}")
    return sdk


def packet_names() -> dict[int, str]:
    from bedrock_protocol import v2193

    names = {}
    for name in dir(v2193):
        value = getattr(v2193, name)
        packet_id = getattr(value, "packet_id", None)
        if isinstance(packet_id, int):
            names[packet_id] = name.removesuffix("Packet")
    # These names differ between protocol schema and the Go packet pool.
    aliases = {"ServerboundLoadingScreen": "ServerBoundLoadingScreen"}
    names = {packet_id: aliases.get(name, name) for packet_id, name in names.items()}
    return names
