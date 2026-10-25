# SPDX-License-Identifier: Apache-2.0
from __future__ import annotations

import copy
import json
import socket
import subprocess
from pathlib import Path
from types import SimpleNamespace

import psutil
import pytest

from bedrock_bench import bots, config, phases, python_bots
from bedrock_bench.adapters import create
from bedrock_bench.model import Arm
from workloads import WORKLOAD, with_overrides


def managed_workload(**settings):
    return with_overrides(
        WORKLOAD,
        {
            "bots": {
                "implementation": "python",
                "transport": "nethernet",
                "server_key_sha256": "",
                "server_key_source": "managed-local",
                **settings,
            }
        },
    )


def binary():
    path = Path("installed/python.exe")
    return bots.BotBinary(
        "python",
        path,
        "a" * 64,
        bots.Target.host(),
        {"module": "python", "protocol": 2193, "version": "0.1.0.dev0"},
        argv_prefix=(str(path), "-I", "-m", "bedrock_bench_bot"),
    )


def identity():
    return {
        "verified": True,
        "key_digest": "b" * 64,
        "endpoint": "http://[::1]:19672",
        "pid": 123,
        "create_time": 456.25,
        "verified_at": "2026-10-24T01:00:00+00:00",
    }


def selection(endpoint="http://[::1]:19672"):
    ipv6 = "[" in endpoint
    return {
        "endpoint": endpoint,
        "family": int(socket.AF_INET6 if ipv6 else socket.AF_INET),
        "address": "::1" if ipv6 else "127.0.0.2",
        "port": 19672,
        "fd": 7,
    }


@pytest.mark.parametrize(
    "settings,message",
    [
        ({"implementation": "go"}, "requires Python and NetherNet"),
        ({"transport": "raknet"}, "requires Python and NetherNet"),
        ({"server_key_sha256": "b" * 64}, "mutually exclusive"),
        ({"server_key_source": "auto"}, "must be one of"),
    ],
)
def test_managed_configuration_rejects_other_trust_modes(settings, message):
    with pytest.raises(config.ConfigError, match=message):
        managed_workload(**settings)


def test_defaults_and_configured_pin_still_require_explicit_trust():
    assert WORKLOAD.bots.server_key_source == "configured"
    assert managed_workload().bots.server_key_sha256 == ""
    with pytest.raises(config.ConfigError, match="requires a verified server_key_sha256"):
        managed_workload(server_key_source="configured")


def pending(monkeypatch):
    monkeypatch.delenv(phases.ENV_BOT_CMD, raising=False)
    monkeypatch.setattr(bots, "locate", lambda module: binary())
    machine = config.default_machine()
    adapter = create(Arm("bds@1.26.52.3", "bds", "1.26.52.3"), machine)
    return phases.bot_command(adapter, managed_workload(), machine)


def test_pending_command_keeps_preflight_identity_without_claiming_launched_argv(monkeypatch):
    command = pending(monkeypatch)
    assert command.argv == () and command.provenance["argv"] == []
    assert command.provenance["sha256"] == binary().sha256
    assert command.provenance["argv_prefix"] == list(binary().argv_prefix)
    assert command.provenance["server_identity"] == {"status": "pending", "source": "managed-local"}
    assert "-server-key-sha256" not in command.pending_args


def test_managed_mode_rejects_command_override_before_locating(monkeypatch):
    monkeypatch.setenv(phases.ENV_BOT_CMD, '["unmanaged"]')
    monkeypatch.setattr(bots, "locate", lambda module: pytest.fail("override must fail before locate"))
    machine = config.default_machine()
    adapter = create(Arm("bds@1.26.52.3", "bds", "1.26.52.3"), machine)
    with pytest.raises(config.ConfigError, match="cannot use.*BBENCH_BOT_CMD"):
        phases.bot_command(adapter, managed_workload(), machine)


def session(command):
    value = phases.Session.__new__(phases.Session)
    value.bot = command
    value.server = SimpleNamespace(pid=123, create_time=lambda: 456.25)
    value.adapter = SimpleNamespace(machine=SimpleNamespace(port=19672))
    value.record = {"bot": dict(command.provenance)}
    value.result = phases.SessionResult()
    value.now = lambda: 4.0
    value.snapshots = []
    value.save = lambda: value.snapshots.append(copy.deepcopy(value.record))
    return value


@pytest.mark.parametrize(
    "endpoint,addr",
    [
        ("http://[::1]:19672", "[::1]:19672"),
        ("http://127.0.0.2:19672", "127.0.0.2:19672"),
    ],
)
def test_resolution_records_exact_final_command_before_launch(monkeypatch, endpoint, addr):
    value = session(pending(monkeypatch))
    calls = []

    def acquire(found, **kwargs):
        calls.append((found, kwargs))
        return {**identity(), "endpoint": endpoint}

    selected = selection(endpoint)
    selections = []
    monkeypatch.setattr(phases, "_owned_signaling_endpoint", lambda *args: selections.append(args) or selected)
    monkeypatch.setattr(python_bots, "acquire_server_identity", acquire)
    value._resolve_signaling_endpoint(20.0)
    assert len(selections) == 1 and selections[0][1:] == (456.25, 19672)
    assert calls[0][1] == {"pid": 123, "created": 456.25, "listener": selected, "timeout_s": 16.0}
    assert value.bot.argv[-2:] == ("-server-key-sha256", "b" * 64)
    assert value.bot.argv[value.bot.argv.index("-addr") + 1] == addr
    assert value.record["bot"]["argv"] == list(value.bot.argv)
    assert value.record["bot"]["server_identity"]["source"] == "managed-local-process"
    assert value.snapshots[-1]["bot"] == value.record["bot"]
    schema = config.load_schema("run")
    pytest.importorskip("jsonschema").validate(
        value.record["bot"],
        {"$defs": schema["$defs"], **schema["properties"]["bot"]},
    )


def test_failed_probe_keeps_argv_empty_and_never_reaches_idle_or_join(monkeypatch):
    value = session(pending(monkeypatch))
    monkeypatch.setattr(phases, "_owned_signaling_endpoint", lambda *args: selection())
    monkeypatch.setattr(
        python_bots,
        "acquire_server_identity",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            bots.BotError("managed-local server identity failed: server_listener_changed")
        ),
    )
    value._start = lambda: None
    value._boot = lambda: value._resolve_signaling_endpoint(20.0)
    value._idle = lambda: pytest.fail("identity failure must precede idle")
    value._join = lambda: pytest.fail("identity failure must not launch bots")
    value.progress = lambda text: None
    value._teardown = lambda: None
    value._collect = lambda: None
    value.run()
    assert value.record["bot"]["argv"] == [] and value.bot.argv == ()
    assert value.record["bot"]["server_identity"]["status"] == "failed"
    assert any("server_listener_changed" in reason for reason in value.result.reasons)


def test_probe_invocation_uses_installed_interpreter_and_boot_bound(monkeypatch):
    calls = []

    def run(command, **kwargs):
        calls.append((command, kwargs))
        return subprocess.CompletedProcess(command, 0, json.dumps(identity()).encode(), b"unrecorded diagnostic")

    monkeypatch.setattr(python_bots.subprocess, "run", run)
    result = python_bots.acquire_server_identity(binary(), pid=123, created=456.25, listener=selection(), timeout_s=3.0)
    assert result == identity()
    command, kwargs = calls[0]
    assert command[:4] == [str(binary().path), "-I", "-m", "bedrock_bench_bot.server_identity"]
    assert command[command.index("--server-pid") + 1] == "123"
    for flag, value in (
        ("--endpoint", "http://[::1]:19672"),
        ("--listener-address", "::1"),
        ("--listener-fd", "7"),
    ):
        assert command[command.index(flag) + 1] == value
    assert float(command[command.index("--timeout") + 1]) < 3.0
    assert kwargs["timeout"] == 3.0


@pytest.mark.parametrize(
    "changed",
    [
        {"verified": False},
        {"pid": 124},
        {"create_time": 457.25},
        {"endpoint": "http://localhost:19672"},
        {"endpoint": "http://192.0.2.1:19672"},
        {"endpoint": "http://[::1]:1234"},
        {"endpoint": "http://127.0.0.2:19672"},
        {"key_digest": "x" * 64},
        {"verified_at": "2026-10-24T01:00:00"},
    ],
)
def test_probe_result_requires_exact_local_process_and_verified_digest(monkeypatch, changed):
    monkeypatch.setattr(
        python_bots.subprocess,
        "run",
        lambda command, **kwargs: subprocess.CompletedProcess(
            command, 0, json.dumps({**identity(), **changed}).encode(), b""
        ),
    )
    with pytest.raises(bots.BotError, match="failed verification checks"):
        python_bots.acquire_server_identity(binary(), pid=123, created=456.25, listener=selection(), timeout_s=3.0)


def test_probe_errors_do_not_echo_http_sdp_or_token_text(monkeypatch):
    monkeypatch.setattr(
        python_bots.subprocess,
        "run",
        lambda command, **kwargs: subprocess.CompletedProcess(
            command, 1, b'{"verified":false,"error":"sensitive SDP/token text"}', b"sensitive SDP/token text"
        ),
    )
    with pytest.raises(bots.BotError, match="probe_failed") as error:
        python_bots.acquire_server_identity(binary(), pid=123, created=456.25, listener=selection(), timeout_s=3.0)
    assert "sensitive" not in str(error.value)


def go_binary():
    return bots.BotBinary(
        "loadbot", Path("installed/loadbot.exe"), "a" * 64, bots.Target.host(), {"module": "loadbot", "protocol": 2193}
    )


def go_command(monkeypatch, *, transport="nethernet", server="bds"):
    monkeypatch.setattr(bots, "locate", lambda module: go_binary())
    machine = config.default_machine()
    version = "1.26.52.3" if server == "bds" else "0.11.14"
    adapter = create(Arm(f"{server}@{version}", server, version), machine)
    workload = with_overrides(WORKLOAD, {"bots": {"transport": transport}})
    return phases.bot_command(adapter, workload, machine)


@pytest.mark.parametrize("implementation,transport", [("go", "raknet"), ("go", "nethernet"), ("python", "nethernet")])
def test_transport_selection_uses_adapter_capability_not_name(monkeypatch, implementation, transport):
    monkeypatch.delenv(phases.ENV_BOT_CMD, raising=False)
    monkeypatch.setattr(bots, "locate", lambda module: binary() if module == "python" else go_binary())
    machine = config.default_machine()
    adapter = SimpleNamespace(name="fixture", protocol=2193, supports_nethernet=True, bot_compat_flags=())
    workload = with_overrides(
        WORKLOAD,
        {
            "bots": {
                "implementation": implementation,
                "transport": transport,
                "server_key_sha256": "a" * 64 if implementation == "python" else "",
            }
        },
    )
    command = phases.bot_command(adapter, workload, machine)
    assert bool(command.pending_args) == (implementation == "go" and transport == "nethernet")
    adapter.supports_nethernet = False
    if transport == "nethernet":
        with pytest.raises(bots.BotError, match="no supported NetherNet"):
            phases.bot_command(adapter, workload, machine)
    else:
        assert phases.bot_command(adapter, workload, machine) == command


def listener(address, *, pid=123, fd=7):
    return SimpleNamespace(
        family=socket.AF_INET6 if ":" in address else socket.AF_INET,
        laddr=SimpleNamespace(ip=address, port=19672),
        status=psutil.CONN_LISTEN,
        pid=pid,
        fd=fd,
    )


def listeners(monkeypatch, owned, *, inventory=None, created=456.25, legacy_api=False):
    process = SimpleNamespace(create_time=lambda: created, status=lambda: "running", is_running=lambda: True)
    setattr(process, "connections" if legacy_api else "net_connections", lambda **kwargs: owned)
    monkeypatch.setattr(phases.psutil, "Process", lambda pid: process)
    monkeypatch.setattr(phases.psutil, "net_connections", lambda **kwargs: owned if inventory is None else inventory)
    return process


@pytest.mark.parametrize(
    "owned,endpoint,addr",
    [
        ([listener("::")], "http://[::1]:19672", "[::1]:19672"),
        ([listener("::1")], "http://[::1]:19672", "[::1]:19672"),
        ([listener("127.0.0.2")], "http://127.0.0.2:19672", "127.0.0.2:19672"),
        ([listener("0.0.0.0")], "http://127.0.0.1:19672", "127.0.0.1:19672"),
        ([listener("0.0.0.0"), listener("::", fd=8)], "http://[::1]:19672", "[::1]:19672"),
    ],
)
@pytest.mark.parametrize("server", ["bds", "endstone"])
def test_go_nethernet_uses_owned_endpoint_and_actual_recorded_argv(monkeypatch, owned, endpoint, addr, server):
    monkeypatch.delenv(phases.ENV_BOT_CMD, raising=False)
    command = go_command(monkeypatch, server=server)
    assert command.argv == () and command.provenance["argv"] == []
    value = session(command)
    listeners(monkeypatch, owned)
    monkeypatch.setattr(
        python_bots, "acquire_server_identity", lambda *args, **kwargs: pytest.fail("Go has no crypto probe")
    )
    value._resolve_signaling_endpoint()
    assert value.bot.argv[value.bot.argv.index("-addr") + 1] == addr
    assert value.record["bot"]["argv"] == list(value.bot.argv)
    assert value.record["bot"]["signaling_endpoint"] == endpoint
    assert "server_identity" not in value.record["bot"] and "-server-key-sha256" not in value.bot.argv


@pytest.mark.parametrize(
    "owned,inventory,created,message",
    [
        ([], [], 456.25, "no owned local signaling listener"),
        ([listener("192.0.2.1")], None, 456.25, "no owned local signaling listener"),
        ([listener("::")], [listener("::", pid=None)], 456.25, "unknown or ambiguous"),
        ([listener("::")], [listener("::", pid=124)], 456.25, "unknown or ambiguous"),
        ([listener("::")], None, 457.25, "process identity mismatch"),
    ],
)
def test_go_endpoint_missing_or_wrong_owner_rejects(monkeypatch, owned, inventory, created, message):
    listeners(monkeypatch, owned, inventory=inventory, created=created)
    with pytest.raises(bots.BotError, match=message):
        phases._owned_signaling_endpoint(SimpleNamespace(pid=123), 456.25, 19672)


def test_go_endpoint_keeps_existing_psutil59_compatibility(monkeypatch):
    listeners(monkeypatch, [listener("::")], legacy_api=True)
    assert phases._owned_signaling_endpoint(SimpleNamespace(pid=123), 456.25, 19672) == {
        **selection(),
        "address": "::",
    }


def test_go_missing_listener_fails_before_idle_or_bot_launch(monkeypatch):
    monkeypatch.delenv(phases.ENV_BOT_CMD, raising=False)
    value = session(go_command(monkeypatch))
    listeners(monkeypatch, [])
    value._start = lambda: None
    value._boot = value._resolve_signaling_endpoint
    value._idle = lambda: pytest.fail("failed endpoint resolution must precede idle")
    value._join = lambda: pytest.fail("failed endpoint resolution must not launch bots")
    value._teardown = lambda: None
    value._collect = lambda: None
    value.progress = lambda text: None
    value.run()
    assert value.record["bot"]["argv"] == []
    assert any("no owned local signaling listener" in reason for reason in value.result.reasons)


def test_go_override_and_default_raknet_and_python_configured_addresses_stay_unchanged(monkeypatch):
    monkeypatch.setenv(phases.ENV_BOT_CMD, '["custom-bot"]')
    command = go_command(monkeypatch)
    value = session(command)
    monkeypatch.setattr(
        phases, "_owned_signaling_endpoint", lambda *args: pytest.fail("override must not be rewritten")
    )
    value._resolve_signaling_endpoint()
    assert value.bot == command and not value.snapshots
    assert command.argv[command.argv.index("-addr") + 1] == "127.0.0.1:19672"
    monkeypatch.delenv(phases.ENV_BOT_CMD)
    default = go_command(monkeypatch, transport="raknet")
    assert default.argv and default.binary is None
    assert default.provenance["argv"][0] == "loadbot"
    assert default.argv[default.argv.index("-addr") + 1] == "127.0.0.1:19672"
    monkeypatch.setattr(bots, "locate", lambda module: binary())
    machine = config.default_machine()
    adapter = create(Arm("bds@1.26.52.3", "bds", "1.26.52.3"), machine)
    configured = phases.bot_command(
        adapter, managed_workload(server_key_source="configured", server_key_sha256="c" * 64), machine
    )
    assert configured.argv and configured.binary is None
    assert configured.argv[configured.argv.index("-addr") + 1] == "127.0.0.1:19672"
    assert configured.argv[-2:] == ("-server-key-sha256", "c" * 64)
