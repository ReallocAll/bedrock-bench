# SPDX-License-Identifier: Apache-2.0
"""Selection and installed-wheel provenance; fixture wheels do not implement a network client."""

from __future__ import annotations

import hashlib
import io
import json
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from bedrock_bench import bots, config, python_bots, runner, templates
from bedrock_bench.adapters import create
from bedrock_bench.model import Arm, ExitCode
from bedrock_bench.phases import ENV_BOT_CMD, bot_command
from workloads import WORKLOAD, with_overrides


def python_workload() -> Any:
    return with_overrides(
        WORKLOAD,
        {
            "bots": {"implementation": "python", "transport": "nethernet", "server_key_sha256": "a" * 64},
        },
    )


def python_raknet_workload(server_key_sha256: str = "") -> Any:
    return with_overrides(
        WORKLOAD,
        {
            "bots": {
                "implementation": "python",
                "transport": "raknet",
                "server_key_source": "configured",
                "server_key_sha256": server_key_sha256,
            },
        },
    )


def test_selection_keeps_both_go_modules_and_rejects_unsupported_transports() -> None:
    params = WORKLOAD.bots
    assert bots.module_for_workload(2193, params, supports_nethernet=True) == "loadbot"
    assert bots.module_for_workload(1001, params, supports_nethernet=False) == "legacy1001"
    python = python_workload().bots
    assert bots.module_for_workload(2193, python, supports_nethernet=True) == "python"
    python_raknet = python_raknet_workload().bots
    assert bots.module_for_workload(2193, python_raknet, supports_nethernet=False) == "python"
    with pytest.raises(bots.BotError, match="protocol 2193"):
        bots.module_for_workload(1001, python_raknet, supports_nethernet=False)
    with pytest.raises(bots.BotError, match="protocol 2193"):
        bots.module_for_workload(1001, python, supports_nethernet=False)
    with pytest.raises(bots.BotError, match="no supported NetherNet"):
        bots.module_for_workload(2193, python, supports_nethernet=False)
    with pytest.raises(bots.BotError, match="protocol 2193"):
        bots.module_for_workload(
            1001, with_overrides(WORKLOAD, {"bots": {"transport": "nethernet"}}).bots, supports_nethernet=True
        )


def test_python_flags_include_transport_and_optional_pin() -> None:
    workload = with_overrides(python_workload(), {"bots": {"server_key_sha256": "A" * 64}})
    args = bots.bot_args(workload.bots, addr="localhost:1", csg=False)
    assert args[args.index("-transport") + 1] == "nethernet"
    assert args[args.index("-server-key-sha256") + 1] == "A" * 64
    go = bots.bot_args(WORKLOAD.bots, addr="localhost:1", csg=False)
    assert go[go.index("-transport") + 1] == "raknet"
    assert "-server-key-sha256" not in go


def test_python_raknet_allows_an_optional_server_key_pin() -> None:
    workload = python_raknet_workload("A" * 64)
    args = bots.bot_args(workload.bots, addr="localhost:1", csg=False)
    assert args[args.index("-transport") + 1] == "raknet"
    assert args[args.index("-server-key-sha256") + 1] == "A" * 64


@pytest.mark.parametrize("server", ["bds", "endstone", "altay"])
@pytest.mark.parametrize("transport", ["raknet", "nethernet"])
def test_server_transport_matches_workload(server: str, transport: str, tmp_path: Path) -> None:
    workload = with_overrides(WORKLOAD, {"bots": {"transport": transport}})
    name = "pocketmine.yml" if server == "altay" else "server.properties"
    fmt = templates.file_format(name)
    key = "network.transport" if server == "altay" else "transport"
    variables = {
        "workload": workload,
        "run": {"port": 19672, "port_v6": 19673, "server_cpu_count": 4},
    }
    written = templates.apply(config.server_dir(server), name, tmp_path, variables)
    text = (tmp_path / name).read_text(encoding="utf-8")
    assert templates.values(fmt, text)[key] == transport
    assert templates.readback(tmp_path, [written]) == []


def test_override_keeps_full_prefix_and_hashes_its_files(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    script = tmp_path / "override.py"
    script.write_text("# test command\n", encoding="utf-8")
    prefix = [sys.executable, "-I", str(script)]
    monkeypatch.setenv(ENV_BOT_CMD, json.dumps(prefix))
    machine = config.default_machine()
    adapter = create(Arm("bds@1.26.52.3", "bds", "1.26.52.3"), machine)
    command = bot_command(adapter, python_workload(), machine)
    assert command.override
    assert command.provenance["argv"] == list(command.argv)
    assert command.provenance["argv_prefix"] == prefix
    files = {item["argv_index"]: item for item in command.provenance["executables"]}
    assert files[0]["sha256"] == bots.sha256_file(Path(sys.executable))
    assert files[2]["sha256"] == bots.sha256_file(script)
    schema = config.load_schema("run")
    pytest.importorskip("jsonschema").validate(
        command.provenance, {"$defs": schema["$defs"], **schema["properties"]["bot"]}
    )
    unsupported = create(Arm("pocketmine@5.44.3", "pocketmine", "5.44.3"), machine)
    with pytest.raises(bots.BotError, match="protocol 2193"):
        bot_command(unsupported, python_workload(), machine)


def test_session_selection_checks_workload_even_with_override(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(ENV_BOT_CMD, '["fixture"]')
    workload = python_workload()
    machine = config.default_machine()
    arms, _ = runner.session_arms(["bds@1.26.52.3", "endstone@0.11.14"], machine, workload)
    assert [arm.server for arm in arms] == ["bds", "endstone"]
    with pytest.raises(bots.BotError, match="no supported NetherNet"):
        runner.session_arms(["nukkit@b3caad6"], machine, workload)
    arms, skipped = runner.session_arms([], machine, workload)
    selected = {arm.server for arm in arms}
    assert {"bds", "endstone"}.issubset(selected)
    assert "nukkit" not in selected
    assert any("nukkit" in reason and "no supported NetherNet" in reason for reason in skipped)


def test_unsupported_session_selection_returns_an_environment_error(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(runner.config, "load_workload", lambda path: python_workload())
    args = SimpleNamespace(config="smoke", machine=None, tags=["nukkit@b3caad6"])
    assert runner.run_session(args) == ExitCode.ENV_GATE
    assert "no supported NetherNet" in capsys.readouterr().err


def test_schema_accepts_unknown_bytes_and_python_build(project_root: Path, schema_validator: Any) -> None:
    events = [json.loads(line) for line in (project_root / "bots/core/testdata/golden.jsonl").read_text().splitlines()]
    hello = next(event for event in events if event["ev"] == "hello")
    hello["build"]["module"] = "python"
    schema_validator("bot-event").validate(hello)
    stat = next(event for event in events if event["ev"] == "stat")
    stat.update(rx_bytes=None, tx_bytes=None)
    schema_validator("bot-event").validate(stat)
    stat["rx_bytes"] = -1
    assert not schema_validator("bot-event").is_valid(stat)


def fixture_wheel(folder: Path, name: str, version: str, files: dict[str, str], *, requires: str = "") -> Path:
    """A real installable wheel with fake package contents, for the packaging boundary only."""
    distribution = name.replace("-", "_")
    metadata = f"{distribution}-{version}.dist-info"
    files = {
        **files,
        f"{metadata}/METADATA": f"Metadata-Version: 2.1\nName: {name}\nVersion: {version}\n{requires}\n",
        f"{metadata}/WHEEL": "Wheel-Version: 1.0\nGenerator: test\nRoot-Is-Purelib: true\nTag: py3-none-any\n",
    }
    files[f"{metadata}/RECORD"] = "".join(f"{file},,\n" for file in [*files, f"{metadata}/RECORD"])
    path = folder / f"{distribution}-{version}-py3-none-any.whl"
    with zipfile.ZipFile(path, "w") as wheel:
        for file, value in files.items():
            wheel.writestr(file, value)
    return path


@pytest.mark.parametrize(
    ("base_url", "env_base", "interpreter", "tag"),
    [
        (None, None, None, None),
        (None, "https://mirror.invalid/bots", None, None),
        ("https://override.invalid/bots/", "https://ignored.invalid", "3.13", "v0.8.0"),
    ],
)
def test_python_release_fetch_verifies_wheel_and_records_install_provenance(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    base_url: str | None,
    env_base: str | None,
    interpreter: str | None,
    tag: str | None,
) -> None:
    monkeypatch.setenv("UV", "uv")
    monkeypatch.setenv(bots.ENV_RELEASE_TAG, "v0.9.0")
    monkeypatch.delenv(bots.ENV_RELEASE_URL, raising=False)
    if env_base:
        monkeypatch.setenv(bots.ENV_RELEASE_URL, env_base)
    name = "bedrock_bench_bot-0.1.0.dev0-py3-none-any.whl"
    payload = b"verified synthetic adapter wheel"
    digest = hashlib.sha256(payload).hexdigest()
    requests = []

    def urlopen(url: str, **kwargs: Any) -> io.BytesIO:
        requests.append(url)
        return io.BytesIO(f"{digest}  {name}\n".encode() if url.endswith("SHA256SUMS") else payload)

    path = bots.binary_path("python", root=tmp_path)
    prefix = (str(path), "-I", "-m", "bedrock_bench_bot")
    monkeypatch.setattr(
        python_bots,
        "locate_python",
        lambda **kwargs: bots.BotBinary("python", path, "a" * 64, bots.Target.host(), {}, "installed", prefix),
    )
    calls = []

    def run(command: list[str], **kwargs: Any) -> None:
        calls.append(command)
        assert len(requests) == 2, "verification must finish before any environment command"
        if command[1] == "venv":
            path.parent.mkdir(parents=True)
        if command[1:3] == ["pip", "install"]:
            downloaded = next(Path(value) for value in command if value.endswith(".whl"))
            assert downloaded.name == name and downloaded.read_bytes() == payload
            assert "bedrock-bench-bot==0.1.0.dev0" not in command

    binary = bots.fetch(
        "python", root=tmp_path, tag=tag, base_url=base_url, urlopen=urlopen, python=interpreter, run=run
    )
    base = (base_url or env_base or "https://github.com/ReallocAll/bedrock-bench/releases/download").rstrip("/")
    selected_tag = tag or "v0.9.0"
    assert requests == [f"{base}/{selected_tag}/SHA256SUMS", f"{base}/{selected_tag}/{name}"]
    assert binary.source == "release" and binary.command([]) == list(prefix)
    assert calls[0][calls[0].index("--python") + 1] == (interpreter or "3.12")
    assert calls[-1][1:3] == ["pip", "check"]
    manifest = json.loads(path.with_name(path.name + ".json").read_text())
    assert manifest["source"] == "release" and manifest["requirements"] == []
    assert (manifest["url"], manifest["tag"], manifest["wheel_sha256"]) == (requests[-1], selected_tag, digest)


@pytest.mark.parametrize("failure", ["missing_sums", "missing_entry", "missing_wheel", "bad_checksum"])
def test_python_release_errors_preserve_existing_environment_without_index_fallback(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure: str,
) -> None:
    name = "bedrock_bench_bot-0.1.0.dev0-py3-none-any.whl"
    payload = b"synthetic wheel"
    digest = "0" * 64 if failure == "bad_checksum" else hashlib.sha256(payload).hexdigest()
    files = {"SHA256SUMS": f"{digest}  {name}\n".encode(), name: payload}
    if failure == "missing_entry":
        files["SHA256SUMS"] = b""
    elif failure == "missing_sums":
        del files["SHA256SUMS"]
    elif failure == "missing_wheel":
        del files[name]
    path = bots.binary_path("python", root=tmp_path)
    path.parent.mkdir(parents=True)
    path.write_bytes(b"existing interpreter")
    manifest = path.with_name(path.name + ".json")
    manifest.write_bytes(b"existing manifest")

    def urlopen(url: str, **kwargs: Any) -> io.BytesIO:
        asset = url.rsplit("/", 1)[1]
        if asset not in files:
            raise OSError("404 fixture")
        return io.BytesIO(files[asset])

    def unexpected_run(*args: Any, **kwargs: Any) -> None:
        pytest.fail("unverified release must not run the installer")

    with pytest.raises(bots.BotError):
        bots.fetch("python", root=tmp_path, tag="v0.1.0", urlopen=urlopen, run=unexpected_run)
    assert path.read_bytes() == b"existing interpreter" and manifest.read_bytes() == b"existing manifest"


def test_fetch_installs_wheels_and_locate_detects_dependency_file_drift(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    uv = shutil.which("uv")
    if uv is None:
        pytest.skip("uv is required for the installed fixture-wheel check")
    monkeypatch.setenv("UV", uv)
    # This test mutates installed files; never share its wheel cache or linked files.
    monkeypatch.setenv("UV_CACHE_DIR", str(tmp_path / "uv-cache"))
    monkeypatch.setenv("UV_LINK_MODE", "copy")
    wheels = tmp_path / "wheels"
    wheels.mkdir()
    fixture_wheel(
        wheels,
        "endpilot",
        "0.1.0",
        {"endpilot/__init__.py": "# fake native dependency\n", "endpilot/data.bin": "actual resource bytes\n"},
    )
    fixture_wheel(
        wheels,
        "bedrock-bench-bot",
        "0.1.0.dev0",
        {
            "bedrock_bench_bot/__init__.py": "__version__ = '0.1.0.dev0'\n",
            "bedrock_bench_bot/identity.py": (
                config.project_root() / "bots/python/src/bedrock_bench_bot/identity.py"
            ).read_text(encoding="utf-8"),
            "bedrock_bench_bot/__main__.py": (
                "import json\nfrom .identity import build_info\nprint(json.dumps(build_info()))\n"
            ),
        },
        requires="Requires-Dist: endpilot==0.1.0\n",
    )
    binary = bots.fetch("python", root=tmp_path, python=sys.executable, find_links=[str(wheels)], no_index=True)
    assert binary.command(["-version"]) == [str(binary.path), "-I", "-m", "bedrock_bench_bot", "-version"]
    assert binary.source == "fetch"
    assert not (tmp_path / "bots/python").exists(), "fetch must not use checkout source"
    environment = binary.info["environment"]
    assert environment["interpreter"]["sha256"] == bots.sha256_file(binary.path)
    distributions = {item["name"]: item for item in environment["distributions"]}
    assert distributions["endpilot"]["version"] == "0.1.0"
    assert "endpilot/__init__.py" in distributions["endpilot"]["files"]
    assert "endpilot/data.bin" in distributions["endpilot"]["files"]
    assert "dependencies" not in binary.info and "python" not in binary.info
    record = binary.provenance(["-transport", "nethernet"])
    run_schema = config.load_schema("run")
    import jsonschema

    jsonschema.validate(record, {"$defs": run_schema["$defs"], **run_schema["properties"]["bot"]})
    assert bots.locate("python", root=tmp_path).sha256 == binary.sha256
    site = binary.path.parent.parent / (
        "Lib/site-packages"
        if sys.platform == "win32"
        else f"lib/python{sys.version_info.major}.{sys.version_info.minor}/site-packages"
    )
    dependency = site / "endpilot/__init__.py"
    dependency.write_text("# installed file changed without changing interpreter or version\n", encoding="utf-8")
    drifted = bots.locate("python", root=tmp_path)
    assert drifted.sha256 != binary.sha256
    assert drifted.source == "installed"
    assert drifted.info["environment"]["interpreter"] == environment["interpreter"]
    resource = site / "endpilot/data.bin"
    resource.unlink()
    with pytest.raises(bots.BotError, match="-version failed"):
        bots.locate("python", root=tmp_path)
    resource.write_text("actual resource bytes\n", encoding="utf-8")
    metadata = site / "endpilot-0.1.0.dist-info/METADATA"
    metadata.write_text(metadata.read_text().replace("Version: 0.1.0", "Version: 9.0"), encoding="utf-8")
    with pytest.raises(bots.BotError, match="requires installed endpilot"):
        bots.locate("python", root=tmp_path)
    incompatible = tmp_path / "incompatible-requirements.txt"
    incompatible.write_text("endpilot==9.9.9\n", encoding="utf-8")
    with pytest.raises(bots.ToolchainError, match="pip install failed with exit code"):
        bots.fetch(
            "python",
            root=tmp_path,
            python=sys.executable,
            find_links=[str(wheels)],
            no_index=True,
            requirements=[incompatible],
        )
    assert not binary.path.with_name(binary.path.name + ".json").exists()
    with pytest.raises(bots.BotError, match="-version failed"):
        bots.locate("python", root=tmp_path)


@pytest.mark.parametrize("action", ["build", "fetch", "locate"])
def test_python_rejects_cross_target_before_mutation(tmp_path: Path, action: str) -> None:
    cross = bots.Target("linux", "arm64")
    if cross == bots.Target.host():
        cross = bots.Target("windows", "amd64")
    with pytest.raises(bots.BotError, match="host-only"):
        getattr(bots, action)("python", target=cross, root=tmp_path)
    assert not list(tmp_path.iterdir())


def test_failed_python_install_is_reported_without_source_fallback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("UV", "uv")
    calls = []

    def fail_install(command: list[str], **kwargs: Any) -> None:
        calls.append(command)
        if command[1:3] == ["pip", "install"]:
            raise subprocess.CalledProcessError(42, command)

    with pytest.raises(bots.ToolchainError, match="42"):
        bots.fetch("python", root=tmp_path, run=fail_install, no_index=True)
    assert calls[0][calls[0].index("--python") + 1] == "3.12"
    assert "--only-binary" in calls[-1] and ":all:" in calls[-1]
    assert calls[-1][calls[-1].index("--no-binary") + 1] == "nethernet"
    assert "bedrock-bench-bot==0.1.0.dev0" in calls[-1]
    assert not any("build" in command for command in calls)


def test_build_installs_wheel_and_records_caller_requirements(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    caller = tmp_path / "caller"
    caller.mkdir()
    first = caller / "trusted SDK.txt"
    second = caller / "other.txt"
    first.write_text("example==1.2.3\n", encoding="utf-8")
    second.write_text("other==4.5.6\n", encoding="utf-8")
    monkeypatch.chdir(caller)
    monkeypatch.setenv("UV", "uv")
    root = tmp_path / "checkout"
    src = root / "bots/python"
    src.mkdir(parents=True)
    (src / "pyproject.toml").write_text("# source fixture\n")
    path = bots.binary_path("python", root=root)
    prefix = (str(path), "-I", "-m", "bedrock_bench_bot")
    monkeypatch.setattr(
        python_bots,
        "locate_python",
        lambda **kwargs: bots.BotBinary("python", path, "a" * 64, bots.Target.host(), {}, "installed", prefix),
    )
    calls = []

    def fake_run(command: list[str], **kwargs: Any) -> None:
        calls.append((command, kwargs))
        assert kwargs["cwd"] == root
        if command[1] == "build":
            output = Path(command[command.index("--out-dir") + 1])
            (output / "bedrock_bench_bot-0.1.0.dev0-py3-none-any.whl").write_bytes(b"fixture wheel")
        if command[1] == "venv":
            path.parent.mkdir(parents=True)

    binary = bots.build(
        "python",
        root=root,
        requirements=[Path(first.name), Path(second.name)],
        run=fake_run,
    )
    assert binary.source == "build"
    install = next(command for command, _ in calls if command[1:3] == ["pip", "install"])
    assert any(value.endswith(".whl") for value in install)
    assert "bedrock-bench-bot==0.1.0.dev0" not in install
    locations = [install[index + 1] for index, value in enumerate(install) if value == "--requirement"]
    assert locations == [str(first), str(second)]
    assert all(Path(location).is_absolute() for location in locations)
    assert "--only-binary" in install and ":all:" in install
    assert install[install.index("--no-binary") + 1] == "nethernet"
    manifest = json.loads(path.with_name(path.name + ".json").read_text())
    assert manifest["requirements"] == [
        {"path": str(first), "sha256": bots.sha256_file(first)},
        {"path": str(second), "sha256": bots.sha256_file(second)},
    ]


@pytest.mark.parametrize("action,requirement", [("build", "missing.txt"), ("fetch", ".")])
def test_invalid_requirement_fails_before_installation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    action: str,
    requirement: str,
) -> None:
    monkeypatch.chdir(tmp_path)

    def unexpected_run(*args: Any, **kwargs: Any) -> None:
        pytest.fail("invalid requirements must fail before any tool runs")

    with pytest.raises(config.ConfigError, match="requirement file does not exist or is not a file"):
        getattr(bots, action)("python", root=tmp_path, requirements=[Path(requirement)], run=unexpected_run)
    assert not list(tmp_path.iterdir())


@pytest.mark.parametrize("action", ["build", "fetch"])
def test_go_rejects_python_requirements(tmp_path: Path, action: str) -> None:
    with pytest.raises(config.ConfigError, match="Python install options require --module python"):
        getattr(bots, action)("loadbot", root=tmp_path, requirements=[Path("trusted.txt")])
    assert not list(tmp_path.iterdir())
