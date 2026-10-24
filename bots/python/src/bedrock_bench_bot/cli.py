# SPDX-License-Identifier: Apache-2.0
from __future__ import annotations

import asyncio
import json
import os
import signal
import stat
import sys
import threading

from .config import UsageError, parse_args
from .events import Emitter, selftest
from .fleet import Fleet
from .identity import build_info, load_sdk, packet_names, running_loop_info


def _stdin(fleet, stdin):
    """Install a bounded stdin reader; Windows pipes cannot use SelectorEventLoop.add_reader."""
    loop = asyncio.get_running_loop()
    active = threading.Event()
    active.set()
    limit = 1024 * 1024

    def line(text):
        text = text.strip()
        if text == "quit":
            fleet.request_stop()
            return False
        if text:
            fleet.log("ignored: stdin accepts plain quit only")
        return True

    def stop_if_active():
        if active.is_set():
            fleet.request_stop()

    def stop_owner():
        if active.is_set() and not loop.is_closed():
            try:
                loop.call_soon_threadsafe(stop_if_active)
            except RuntimeError:
                pass  # The owner may have closed between the check and scheduling.

    def threaded_reader():
        def reader():
            while active.is_set():
                try:
                    text = stdin.readline(limit + 1)
                    if not active.is_set():
                        return
                    if not text or len(text) > limit:
                        stop_owner()
                        return
                    if isinstance(text, bytes):
                        text = text.decode("utf-8", "replace")
                    text = text.strip()
                    if text == "quit":
                        stop_owner()
                        return
                    if text:
                        fleet.log("ignored: stdin accepts plain quit only")
                except (OSError, ValueError):
                    stop_owner()
                    return

        threading.Thread(target=reader, name="bench-stdin", daemon=True).start()
        return active.clear

    if sys.platform == "win32":
        return threaded_reader()

    try:
        fd = stdin.fileno()
        regular = stat.S_ISREG(os.fstat(fd).st_mode)
    except (OSError, ValueError):
        fleet.request_stop()
        return active.clear
    pending = bytearray()

    def remove():
        active.clear()
        if not regular:
            loop.remove_reader(fd)

    def readable():
        if not active.is_set():
            return
        try:
            data = os.read(fd, 65536)
            if not data:
                if pending:
                    line(pending.decode("utf-8", "replace"))
                fleet.request_stop()
                remove()
                return
            pending.extend(data)
            while b"\n" in pending:
                item, _, rest = pending.partition(b"\n")
                pending[:] = rest
                if len(item) > limit or not line(item.decode("utf-8", "replace")):
                    fleet.request_stop()
                    remove()
                    return
            if len(pending) > limit:
                fleet.log("stdin: input line exceeds 1MiB")
                fleet.request_stop()
                remove()
                return
            if regular:
                loop.call_soon(readable)
        except OSError as error:
            fleet.log(f"stdin: {error}")
            fleet.request_stop()
            remove()

    if regular:
        loop.call_soon(readable)
    else:
        try:
            loop.add_reader(fd, readable)
        except PermissionError:
            # epoll rejects nonpollable character devices such as /dev/null.
            return threaded_reader()
        except (OSError, ValueError):
            fleet.request_stop()
            return active.clear
    return remove


async def _run(config, sdk, emitter, build, stdin, stderr):
    build = {**build, "event_loop": running_loop_info()}
    fleet = Fleet(config, sdk, emitter, build, stderr, names=packet_names())
    loop = asyncio.get_running_loop()
    saved = {}
    signals = [signal.SIGINT, signal.SIGTERM]
    if hasattr(signal, "SIGBREAK"):
        signals.append(signal.SIGBREAK)
    for kind in signals:
        saved[kind] = signal.getsignal(kind)
        signal.signal(kind, lambda *_: loop.call_soon_threadsafe(fleet.request_stop))

    def remove_stdin():
        return None

    try:
        remove_stdin = _stdin(fleet, stdin)
        return await fleet.run()
    finally:
        remove_stdin()
        for kind, handler in saved.items():
            signal.signal(kind, handler)


def run(args=None, *, stdin=None, stdout=None, stderr=None) -> int:
    stdin = sys.stdin if stdin is None else stdin
    stdout = sys.stdout if stdout is None else stdout
    stderr = sys.stderr if stderr is None else stderr
    try:
        config = parse_args(list(sys.argv[1:] if args is None else args))
        if not (config.version or config.selftest_output):
            config.validate()
        build = build_info()
        if config.version:
            stdout.write(json.dumps(build, allow_nan=False, separators=(",", ":")) + "\n")
            stdout.flush()
            return 0
        emitter = Emitter(stdout)
        if config.selftest_output:
            selftest(emitter, config, build)
            return 1 if emitter.error else 0
        sdk = load_sdk()
        if sys.platform == "win32":
            return asyncio.run(_run(config, sdk, emitter, build, stdin, stderr), loop_factory=asyncio.SelectorEventLoop)
        if sys.platform == "linux":
            import uvloop

            return asyncio.run(_run(config, sdk, emitter, build, stdin, stderr), loop_factory=uvloop.new_event_loop)
        return asyncio.run(_run(config, sdk, emitter, build, stdin, stderr))
    except SystemExit as error:
        return int(error.code)
    except (UsageError, ValueError, OSError, RuntimeError, ImportError) as error:
        print(f"bedrock-bench-bot: {error}", file=stderr, flush=True)
        return 1


def main():
    raise SystemExit(run())
