# SPDX-License-Identifier: Apache-2.0
from __future__ import annotations

import json
import time


class Emitter:
    def __init__(self, stream):
        self.stream = stream
        self.origin = time.monotonic()
        self.error: OSError | None = None
        self.on_error = None

    def now_ms(self) -> float:
        return (time.monotonic() - self.origin) * 1000

    def emit(self, event: str, **body):
        if self.error is not None:
            return
        row = {"v": 1, "ev": event, "t": time.time_ns() // 1_000_000, "mono_ms": self.now_ms(), **body}
        try:
            self.stream.write(json.dumps(row, ensure_ascii=False, allow_nan=False, separators=(",", ":")) + "\n")
            self.stream.flush()
        except OSError as error:
            self.error = error
            if self.on_error is not None:
                self.on_error()


def selftest(emitter: Emitter, config, build):
    """Synthetic wire-independent output exercise for an installed package."""
    emitter.emit("hello", build=build, flags=config.flags(), intervals_ms=config.intervals())
    emitter.emit("dial", bot="b00", i=0)
    emitter.emit("spawned", bot="b00", i=0, session=1, join_ms=812.5, radius=10, dim=0)
    emitter.emit("join_failed", bot="b01", i=1, stage="spawn", err="deadline expired", session=1, attempt=1, final=True)
    emitter.emit("tp", bot="b00", pos=[1280.5, 301.62, 1280.5], dim=0, n=1)
    emitter.emit("first_chunk", bot="b00", dt_ms=1430)
    emitter.emit(
        "progress",
        rows=[["b00", 112, 98, 1280.5, 301.62, 1280.5, 0, 3, 1], ["b01", 0, 0, None, None, None, None, 0, 0]],
    )
    emitter.emit(
        "stat",
        online=1,
        joined=1,
        failed=1,
        dropped=0,
        left=0,
        rejoins=0,
        deaths=0,
        rx_pkts=12345,
        tx_pkts=2345,
        rx_bytes=None,
        tx_bytes=None,
        corrections=3,
        level_chunks=130,
        subchunks=2400,
        uniq=112,
        in_view=98,
        decode_errors=0,
        kicked=0,
        rejoin_failed=0,
    )
    emitter.emit("pkt", rx={"LevelChunk": 130}, tx={"PlayerAuthInput": 1200, "SubChunkRequest": 130})
    emitter.emit("rtt", n=12, p50_ms=3.1, p99_ms=48.0, max_ms=61.2, lost=0)
    for event in ("died", "respawned", "left", "dropped", "rejoin", "kicked"):
        emitter.emit(event, bot="b00", session=1)
    emitter.emit("dim", bot="b00", session=1, dim=1)
    emitter.emit("decode_error", bot="b00", pkt_id=174, err="synthetic body decode error", total=1)
    emitter.emit("alljoined", joined=1, failed=1)
    emitter.emit(
        "bot_summary", bot="b00", uniq=112, in_view=98, chunks=120, corrections=3, first_seen_ms=[1430, 0, 0, 12, 3, 40]
    )
    emitter.emit("done")
