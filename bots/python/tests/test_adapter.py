# SPDX-License-Identifier: Apache-2.0
from __future__ import annotations

import argparse
import asyncio
import io
import json
import math
import os
import signal
import subprocess
import sys
import threading
from dataclasses import replace
from importlib.resources import files
from pathlib import Path
from types import SimpleNamespace

import pytest
from bedrock_bench_bot import cli
from bedrock_bench_bot.config import Config, UsageError, duration, parse_args
from bedrock_bench_bot.events import Emitter
from bedrock_bench_bot.fleet import Fleet
from bedrock_bench_bot.identity import _direct_source, distribution_identity
from bedrock_bench_bot.workload import ChunkTracker, Walker, bot_uuid, in_view, subchunk_heights
from endpilot import (
    BotState,
    DeathEvent,
    DimensionEvent,
    LatencyEvent,
    RespawnEvent,
    StateEvent,
    Vec3,
)
from fake_sdk import make_sdk

BUILD = {"module": "python", "version": "0.1.0.dev0", "protocol": 2193, "patches": []}
CONFIG = Config(addr="localhost:19132", server_key_sha256="ab" * 32)


def rows(stream):
    return [json.loads(line) for line in stream.getvalue().splitlines()]


def by_event(output, name):
    return [row for row in output if row["ev"] == name]


def test_config_go_spellings_and_rejections():
    parsed = parse_args(
        [
            "-addr",
            "[::1]:19132",
            "-server-key-sha256=" + "ab" * 32,
            "-csg=TRUE",
            "-loadscreen=0",
            "-stagger",
            "1m2.5s",
            "-spawn-timeout",
            "1h2m3s",
        ]
    )
    parsed.validate()
    assert parsed.stagger == 62.5 and parsed.spawn_timeout == 3723
    assert parsed.csg and not parsed.loadscreen
    assert parsed.flags()["spawn-timeout"] == "1h2m3s"
    replace(CONFIG, transport="raknet", server_key_sha256="").validate()
    replace(CONFIG, transport="raknet", server_key_sha256="ab" * 32).validate()
    for value, expected in (("300ms", 0.3), ("1.5h2m", 5520), ("+.5s", 0.5), ("-2µs", -0.000002), ("0", 0)):
        assert duration(value) == expected
    for value in ("", "1", "1d", "1sX", "--1s", "+-1s", "-+1s", "1.2.3s"):
        with pytest.raises(argparse.ArgumentTypeError):
            duration(value)
    for config in (
        replace(CONFIG, control=True),
        replace(CONFIG, n=0),
        replace(CONFIG, server_key_sha256=""),
        replace(CONFIG, transport="raknet", server_key_sha256="bad"),
        replace(CONFIG, radius=97),
        replace(CONFIG, addr="http://localhost:19132"),
        replace(CONFIG, addr="user:pw@host:42"),
        replace(CONFIG, prefix="bad name"),
        replace(CONFIG, stat_interval=0.0001),
    ):
        with pytest.raises(UsageError):
            config.validate()


def test_identity_go_vectors():
    assert bot_uuid("bedrock-bench", "a00") == "916f55f9-f355-5753-a1a2-e86b3ff0ead4"
    assert bot_uuid("bedrock-bench", "a07") == "f772d804-dfc0-5864-aca1-62c2503e67b5"
    assert bot_uuid("bedrock-bench", "b00") == "cfa2ea17-c22d-59c6-8d04-476ee9e828bd"
    assert bot_uuid("a63ab145-9c09-5f88-b7a5-cbc4f1aa9bb9", "b00") == bot_uuid("bedrock-bench", "b00")


@pytest.mark.parametrize(
    ("transport", "pin", "url"),
    [
        ("nethernet", "ab" * 32, "http://localhost:19132"),
        ("raknet", "", "raknet://localhost:19132"),
    ],
)
def test_fleet_options_map_sdk_transport(transport, pin, url):
    config = replace(CONFIG, transport=transport, server_key_sha256=pin)
    config.validate()
    fleet = Fleet(config, make_sdk(), Emitter(io.StringIO()), BUILD, io.StringIO())
    options = fleet._options(7)
    assert options.server_url == url
    assert options.transport == transport
    assert options.protocol == 2193
    assert options.server_key_sha256 == pin


def test_circle_advances_yaw_before_moving_and_preserves_none_yaw():
    walker = Walker("circle", 0)
    dx, dz = walker.step()
    assert dx == pytest.approx(-math.sin(0.01) * 0.2158)
    assert dz == pytest.approx(math.cos(0.01) * 0.2158)
    assert walker.degrees() == pytest.approx(0.01 * 180 / math.pi)
    second = Walker("circle", 1)
    second.step()
    assert second.yaw == pytest.approx(137.508 * math.pi / 180 + 0.01)
    idle = Walker("none", 1)
    assert idle.step() == (0, 0)
    assert idle.degrees() == pytest.approx(137.508)


def test_chunks_ignore_spawn_keep_origin_use_dimension_and_current_disc():
    tracker = ChunkTracker()
    assert not tracker.add((0, 0, 0), 100)
    assert tracker.received == 0
    tracker.start(200)
    assert tracker.add((0, -1, 0), 212.8)
    assert not tracker.add((0, -1, 0), 215)
    assert tracker.add((1, -1, 0), 216.2)
    tracker.start(300)
    assert tracker.origin == 200
    tracker.add((0, 2, 0), 220.9)
    assert tracker.received == 4 and len(tracker.seen) == 3
    assert tracker.deltas(0, 0, 0, 2) == [12, 8]
    assert not in_view((0, 2, 2), 0, 0, 0, 1)  # Square bounds would incorrectly include this.
    assert in_view((0, -1, 0), 0, -0.01, 0, 1)
    assert not in_view((1, -1, 0), 0, -0.01, 0, 1)
    assert subchunk_heights(0, 3) == [-4, -3, -2]
    assert subchunk_heights(1, -1) == list(range(8))
    assert subchunk_heights(2, 100) == list(range(16))
    assert subchunk_heights(99, -1) == list(range(-4, 20))
    assert subchunk_heights(0, 0) == []


def test_installed_identity_and_source_metadata_redact_credentials():
    dist = SimpleNamespace(
        read_text=lambda _: json.dumps(
            {
                "url": "https://username:secret@example.com/repo.git?token=secret#token",
                "vcs_info": {"vcs": "git", "commit_id": "abc", "requested_revision": "tag"},
            }
        )
    )
    source = _direct_source(dist)
    assert source["url"] == "https://example.com/repo.git" and "secret" not in json.dumps(source)
    archive = SimpleNamespace(
        read_text=lambda _: json.dumps(
            {"url": "file:///installed.whl", "archive_info": {"hash": "sha256=" + "ab" * 32}}
        )
    )
    assert _direct_source(archive)["archive_hashes"] == {"sha256": "ab" * 32}
    pinned = SimpleNamespace(
        read_text=lambda _: json.dumps({"url": "file:///installed.whl#sha256=" + "cd" * 32, "archive_info": {}})
    )
    assert _direct_source(pinned)["archive_hashes"] == {"sha256": "cd" * 32}


def test_inventory_hashes_actual_resources_and_fails_on_missing_file_or_inventory(tmp_path):
    from bedrock_bench_bot.identity import sha256

    resource = tmp_path / "payload.dat"
    resource.write_bytes(b"original")
    distribution = SimpleNamespace(
        name="extra-distribution",
        version="1.0",
        locate_file=lambda path: tmp_path / path,
        read_text=lambda name: "payload.dat,sha256=stale,8\n__pycache__/cache.pyc,,\n" if name == "RECORD" else None,
    )
    first = distribution_identity(distribution)
    assert first["files"] == {"payload.dat": sha256(resource)}
    resource.write_bytes(b"changed after installation")
    assert distribution_identity(distribution)["files"] != first["files"]
    resource.unlink()
    with pytest.raises(FileNotFoundError):
        distribution_identity(distribution)
    distribution.read_text = lambda _: None
    with pytest.raises(RuntimeError, match="no file inventory: extra-distribution"):
        distribution_identity(distribution)


def test_cli_flags_errors_exit_one_and_do_not_pollute_stdout():
    for args in (["-control"], ["-bogus"], ["positional"]):
        stdout, stderr = io.StringIO(), io.StringIO()
        assert cli.run(args, stdout=stdout, stderr=stderr) == 1
        assert not stdout.getvalue() and stderr.getvalue()


def test_installed_module_and_console_entrypoints():
    from jsonschema import Draft202012Validator

    schema = files("bedrock_bench").joinpath("schemas/bot-event.schema.json")
    validator = Draft202012Validator(json.loads(schema.read_text(encoding="utf-8")))
    module = subprocess.run(
        [sys.executable, "-I", "-m", "bedrock_bench_bot", "-selftest-output"],
        capture_output=True,
        text=True,
        check=False,
        timeout=15,
    )
    assert module.returncode == 0 and module.stderr == ""
    events = [json.loads(line) for line in module.stdout.splitlines()]
    assert events[0]["ev"] == "hello" and events[-1]["ev"] == "done"
    assert {event["ev"] for event in events} == set(validator.schema["properties"]["ev"]["enum"])
    for event in events:
        validator.validate(event)
    console = Path(sys.executable).parent / (
        "bedrock-bench-bot.exe" if sys.platform == "win32" else "bedrock-bench-bot"
    )
    version = subprocess.run([str(console), "-version"], capture_output=True, text=True, check=False, timeout=15)
    assert version.returncode == 0 and version.stderr == ""
    environment = json.loads(version.stdout)["environment"]
    assert any(item["name"] == "bedrock-bench-bot" for item in environment["distributions"])
    isolated = subprocess.run(
        [sys.executable, "-I", "-m", "bedrock_bench_bot", "-version"],
        capture_output=True,
        text=True,
        check=False,
        timeout=15,
    )
    assert isolated.returncode == 0 and isolated.stderr == ""
    assert json.loads(isolated.stdout) == json.loads(version.stdout)


async def exercise(config=CONFIG, plans=None, action=None, fleet_type=Fleet):
    sdk = make_sdk(plans)
    output = io.StringIO()
    fleet = fleet_type(
        config, sdk, Emitter(output), BUILD, io.StringIO(), names={58: "LevelChunk", 144: "PlayerAuthInput"}
    )
    if action:

        async def controller():
            while not sdk.instances or not sdk.instances[0].bots or not fleet.joined:
                await asyncio.sleep(0)
            await action(fleet, sdk.instances[0])

        controller_task = asyncio.create_task(controller())
    result = await asyncio.wait_for(fleet.run(), 2)
    if action:
        await controller_task
    return result, rows(output), sdk, fleet


class FastRetries(Fleet):
    async def sleep(self, delay):
        if delay == self.retry_delay:
            self.retry_observed = getattr(self, "retry_observed", []) + [delay]
            await asyncio.sleep(0)
            return not self.stop.is_set()
        return await super().sleep(delay)


def test_synthetic_bounded_first_join_retries_and_allfailed_exit():
    code, output, sdk, fleet = asyncio.run(
        exercise(replace(CONFIG, join_retries=1), plans=[{"fail": True}, {"fail": True}], fleet_type=FastRetries)
    )
    assert code == 2 and len(sdk.instances[0].bots) == 2
    assert fleet.retry_observed == [3.0]
    assert [row["final"] for row in by_event(output, "join_failed")] == [False, True]
    assert [row["attempt"] for row in by_event(output, "join_failed")] == [1, 2]
    assert by_event(output, "alljoined")[0]["failed"] == 1
    assert not by_event(output, "bot_summary")


def test_synthetic_attempt_deadline_includes_connect_and_does_not_retry_unrequested():
    config = replace(CONFIG, spawn_timeout=0.01)
    code, output, sdk, _ = asyncio.run(exercise(config, plans=[{"hang": True}]))
    assert code == 2 and len(sdk.instances[0].bots) == 1
    assert by_event(output, "join_failed")[0]["stage"] == "dial"


def test_synthetic_immediate_drop_after_ready_is_not_a_join_failure_or_retry():
    async def check():
        sdk = make_sdk([{"ready_then_fail": True}])
        stream = io.StringIO()
        fleet = Fleet(replace(CONFIG, join_retries=4), sdk, Emitter(stream), BUILD, io.StringIO())

        async def stop_after_drop():
            while not fleet.dropped:
                await asyncio.sleep(0)
            fleet.request_stop()

        stopper = asyncio.create_task(stop_after_drop())
        result = await fleet.run()
        await stopper
        assert result == 0 and len(sdk.instances[0].bots) == 1
        output = rows(stream)
        assert not by_event(output, "join_failed")
        assert by_event(output, "alljoined")[0]["joined"] == 1
        assert by_event(output, "stat")[-1]["dropped"] == 1

    asyncio.run(check())


def test_synthetic_none_input_dimension_death_retry_and_probe(monkeypatch):
    from bedrock_bench_bot import fleet as module

    clock = SimpleNamespace(now=100)
    monkeypatch.setattr(module, "time", SimpleNamespace(monotonic=lambda: clock.now))

    async def action(fleet, runtime):
        bot = runtime.bots[0]
        bot.emit(DimensionEvent(1, Vec3(64, 40, 96)))
        bot.emit(DeathEvent())
        bot.tick()
        assert bot.respawns == bot.probes == 0
        for now in (101, 110, 111, 121, 131, 141):
            clock.now = now
            bot.tick()
        assert bot.respawns == 4
        assert bot.respawn_calls == [False, True, True, True]
        bot.emit(RespawnEvent())
        bot.emit(LatencyEvent(1234))
        fleet.request_stop()

    code, output, sdk, _ = asyncio.run(exercise(replace(CONFIG, walk="none", probe_bots=1), action=action))
    bot = sdk.instances[0].bots[0]
    assert code == 0 and all(frame.position.y == 40 and frame.move.y == 0 for frame in bot.inputs)
    assert bot.probes >= 1
    assert len(by_event(output, "died")) == len(by_event(output, "respawned")) == 1
    assert by_event(output, "rtt")[-1]["p50_ms"] == 1.234
    assert by_event(output, "rtt")[-1]["lost"] == 2


@pytest.mark.parametrize("ended", ["respawn", "session"])
def test_synthetic_respawn_retries_stop_after_respawn_or_session_end(monkeypatch, ended):
    from bedrock_bench_bot import fleet as module

    clock = SimpleNamespace(now=100)
    monkeypatch.setattr(module, "time", SimpleNamespace(monotonic=lambda: clock.now))

    async def action(fleet, runtime):
        bot = runtime.bots[0]
        bot.emit(DeathEvent())
        clock.now = 101
        bot.tick()
        assert bot.respawns == 1 and bot.respawn_calls == [False]
        if ended == "respawn":
            bot.emit(RespawnEvent())
        else:
            bot.emit(StateEvent(BotState.closed, "session ended"))
        clock.now = 111
        bot.tick()
        assert bot.respawns == 1 and bot.respawn_calls == [False]
        fleet.request_stop()

    code, _, _, _ = asyncio.run(exercise(action=action))
    assert code == 0


@pytest.mark.parametrize(
    "kind", [signal.SIGINT, signal.SIGTERM] + ([signal.SIGBREAK] if hasattr(signal, "SIGBREAK") else [])
)
def test_synthetic_cli_signal_handlers_stop_normally_and_restore(monkeypatch, kind):
    installed, calls = {}, []
    monkeypatch.setattr(cli, "packet_names", lambda: {})

    def install(number, handler):
        installed[number] = handler
        calls.append(number)

    monkeypatch.setattr(signal, "signal", install)

    def stdin(fleet, _):
        asyncio.get_running_loop().call_soon(installed[kind], kind, None)
        return lambda: None

    monkeypatch.setattr(cli, "_stdin", stdin)
    stream = io.StringIO()
    code = asyncio.run(cli._run(CONFIG, make_sdk(), Emitter(stream), BUILD, io.StringIO(), io.StringIO()))
    assert code == 0 and rows(stream)[-1]["ev"] == "done"
    # asyncio.Runner may also install its own SIGINT handler before entering the coroutine.
    assert calls.count(kind) >= 2
    assert installed[kind] == signal.getsignal(kind)


def test_nonpollable_reader_cancellation_suppresses_queued_owner_stop(monkeypatch):
    async def check():
        loop = asyncio.get_running_loop()
        queued, stopped, workers = [], [], []
        fleet = SimpleNamespace(request_stop=lambda: stopped.append(True), log=lambda _: None)
        monkeypatch.setattr(cli, "sys", SimpleNamespace(platform="linux"))
        monkeypatch.setattr(loop, "add_reader", lambda *_: (_ for _ in ()).throw(PermissionError()))
        monkeypatch.setattr(loop, "call_soon_threadsafe", lambda callback: queued.append(callback))
        original_thread = threading.Thread

        def thread(*args, **kwargs):
            worker = original_thread(*args, **kwargs)
            workers.append(worker)
            return worker

        monkeypatch.setattr(cli.threading, "Thread", thread)
        with open(os.devnull, encoding="utf-8") as stdin:
            remove = cli._stdin(fleet, stdin)
            assert len(workers) == 1 and workers[0].daemon
            workers[0].join(timeout=1)
            assert not workers[0].is_alive() and len(queued) == 1
            remove()
            queued[0]()
        assert stopped == []

    asyncio.run(check())


def test_closed_stdin_is_normal_stop_before_connection_attempts(monkeypatch):
    async def check():
        monkeypatch.setattr(cli, "sys", SimpleNamespace(platform="linux"))
        monkeypatch.setattr(cli, "packet_names", lambda: {})
        stdin = io.StringIO()
        stdin.close()
        stream = io.StringIO()
        sdk = make_sdk()
        code = await cli._run(CONFIG, sdk, Emitter(stream), BUILD, stdin, io.StringIO())
        assert code == 0 and rows(stream)[-1]["ev"] == "done"
        assert not sdk.instances[0].bots

    asyncio.run(check())


def test_fake_xuid_uses_pinned_sdk_public_option():
    fleet = Fleet(replace(CONFIG, fake_xuid=True), make_sdk(), Emitter(io.StringIO()), BUILD, io.StringIO())
    assert fleet._options(7).xuid == "2535400000000007"
