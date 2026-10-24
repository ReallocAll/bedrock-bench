"""CLI-owned loop selection and shutdown; SDK behavior is covered separately."""

import asyncio
import io
import json
import os
import signal
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
from bedrock_bench_bot import cli
from fake_sdk import make_sdk

ARGS = ["-addr", "127.0.0.1:19132", "-server-key-sha256", "ab" * 32, "-stagger", "0"]
BUILD = {"module": "python", "version": "0.1.0.dev0", "protocol": 2193}


def test_linux_missing_uvloop_does_not_fall_back(monkeypatch):
    monkeypatch.setitem(sys.modules, "uvloop", None)
    monkeypatch.setattr(cli, "sys", SimpleNamespace(platform="linux"))
    monkeypatch.setattr(cli, "load_sdk", lambda: object())
    monkeypatch.setattr(cli, "build_info", lambda: BUILD)
    monkeypatch.setattr(cli.asyncio, "run", lambda *args, **kwargs: pytest.fail("unexpected loop fallback"))
    stderr = io.StringIO()
    assert cli.run(ARGS, stdin=io.StringIO(), stdout=io.StringIO(), stderr=stderr) == 1
    assert "uvloop" in stderr.getvalue()


@pytest.mark.parametrize("kind", ["pipe-quit", "pipe-eof", "pipe-ignored", "regular-quit", "regular-eof", "devnull"])
def test_actual_cli_loop_stdin_stops_with_summary(monkeypatch, tmp_path, kind):
    monkeypatch.setattr(cli, "load_sdk", make_sdk)
    monkeypatch.setattr(cli, "packet_names", lambda: {})
    monkeypatch.setattr(cli, "build_info", lambda: BUILD)
    if kind.startswith("pipe"):
        reader, writer = os.pipe()
        if kind.endswith("quit"):
            os.write(writer, b"quit\n")
        elif kind.endswith("ignored"):
            os.write(writer, b"ignored\nquit\n")
        os.close(writer)
        stdin = os.fdopen(reader, encoding="utf-8")
    elif kind.startswith("regular"):
        path = tmp_path / "stdin.txt"
        path.write_text("quit\n" if kind.endswith("quit") else "")
        stdin = path.open()
    else:
        stdin = open(os.devnull)
    output = io.StringIO()
    diagnostics = io.StringIO()
    with stdin:
        assert cli.run(ARGS, stdin=stdin, stdout=output, stderr=diagnostics) == 0, diagnostics.getvalue()
    rows = [json.loads(line) for line in output.getvalue().splitlines()]
    assert rows[0]["ev"] == "hello" and rows[-1]["ev"] == "done"
    assert [row["ev"] for row in rows if row["ev"] in ("progress", "stat", "pkt")] == ["progress", "stat", "pkt"]
    assert ("ignored: stdin accepts plain quit only" in diagnostics.getvalue()) == kind.endswith("ignored")
    loop = rows[0]["build"]["event_loop"]
    if sys.platform == "linux":
        assert loop == {"module": "uvloop", "class": "Loop", "uvloop_version": "0.23.0"}
    else:
        assert loop["uvloop_version"] is None and loop["module"].startswith("asyncio")
        if sys.platform == "win32":
            assert loop["class"] == asyncio.SelectorEventLoop.__qualname__


@pytest.mark.parametrize("kind", [signal.SIGINT, signal.SIGTERM])
def test_cli_actual_signals_exit_with_summary(kind):
    if sys.platform == "win32":
        pytest.skip("POSIX SIGTERM handling; Windows signal contract uses existing suite")
    code = f"""
import sys
sys.path.insert(0, {str(Path(__file__).parent)!r})
from fake_sdk import make_sdk
from bedrock_bench_bot import cli
cli.load_sdk = make_sdk
cli.packet_names = lambda: {{}}
cli.build_info = lambda: {BUILD!r}
raise SystemExit(cli.run({ARGS!r}))
"""
    child = subprocess.Popen(
        [sys.executable, "-I", "-B", "-c", code],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        first = child.stdout.readline()
        assert first, child.stderr.read()
        assert json.loads(first)["ev"] == "hello"
        child.send_signal(kind)
        # Wait before closing stdin: EOF must not accidentally satisfy this signal test.
        assert child.wait(timeout=10) == 0
        rows = [json.loads(line) for line in (first + child.stdout.read()).splitlines()]
        assert rows[-1]["ev"] == "done" and child.stderr.read() == ""
        assert rows[0]["build"]["event_loop"]["module"] == "uvloop"
    finally:
        if child.poll() is None:
            child.kill()
            child.wait(timeout=5)
        for stream in (child.stdin, child.stdout, child.stderr):
            stream.close()
