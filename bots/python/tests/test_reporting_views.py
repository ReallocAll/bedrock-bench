# SPDX-License-Identifier: Apache-2.0
from __future__ import annotations

import asyncio
import io
from collections import Counter
from dataclasses import replace
from types import SimpleNamespace

from bedrock_bench_bot.events import Emitter
from bedrock_bench_bot.fleet import Attempt, Fleet
from bedrock_bench_bot.workload import ChunkTracker, Walker, in_view
from test_adapter import BUILD, CONFIG, rows


class StatisticsBot:
    def __init__(self, rx, tx, lost, radius=0):
        self.rx, self.tx = [0] * 1024, [0] * 1024
        for values, counts in ((self.rx, rx), (self.tx, tx)):
            for packet_id, count in counts.items():
                values[packet_id] = count
        self.lost, self.radius = lost, radius

    def capture_statistics(self):
        return SimpleNamespace(
            play_packet_counts_in=tuple(self.rx),
            play_packet_counts_out=tuple(self.tx),
            packet_counts_in=(10000,) * 1024,
            packet_counts_out=(10000,) * 1024,
            probes_lost=self.lost,
        )

    def get_status(self):
        return SimpleNamespace(chunk_radius=self.radius)


def test_coincident_reports_preserve_promoted_counts_failed_exclusion_and_reset_windows():
    stream = io.StringIO()
    emitter = Emitter(stream)
    config = replace(CONFIG, progress_interval=0.02, stat_interval=0.02, pkt_interval=0.02, probe_bots=1)
    fleet = Fleet(
        config,
        None,
        emitter,
        BUILD,
        io.StringIO(),
        names={5: "Disconnect", 70: "Radius", 115: "Cache", 144: "Input", 174: "Chunk", 175: "Request"},
    )
    first = Attempt(
        0,
        "b00",
        Walker("none", 0),
        0,
        bot=StatisticsBot({5: 40, 174: 3, 70: 2}, {144: 11, 175: 2}, 2),
        spawned=True,
        online=True,
        startup_rx=Counter({174: 2}),
        startup_tx=Counter({115: 1}),
    )
    second = Attempt(
        1,
        "b01",
        Walker("none", 1),
        0,
        bot=StatisticsBot({5: 1, 70: 4}, {144: 5}, 3),
        spawned=True,
        startup_rx=Counter({70: 1}),
    )
    failed = Attempt(
        2, "b02", Walker("none", 2), 0, bot=StatisticsBot({70: 900}, {144: 900}, 900), startup_rx=Counter({174: 900})
    )
    fleet.attempts = [first, second, failed]
    fleet.last_spawned = {1: second, 0: first}
    fleet.started_indices = [0, 1, 2]
    fleet.joined, fleet.failed, fleet.tick_lateness_us_max = 2, 1, 170
    fleet.rtts = [3, 1, 2]
    retained = first.bot.capture_statistics()

    # Let all three deadlines elapse before one real reporting pass.
    async def once(delay):
        await asyncio.sleep(delay + 0.01)
        fleet.stop.set()
        return True

    fleet.sleep = once
    asyncio.run(fleet.report())
    output = rows(stream)
    assert [row["ev"] for row in output] == ["progress", "stat", "rtt", "pkt"]
    stat = output[1]
    assert (stat["online"], stat["joined"], stat["failed"], stat["rx_pkts"], stat["tx_pkts"]) == (1, 2, 1, 12, 19)
    assert list(stat["input_packets"].items()) == [("b00", 11), ("b01", 5)]
    assert stat["tick_lateness_us_max"] == 170
    assert {key: output[2][key] for key in ("n", "p50_ms", "p99_ms", "max_ms", "lost")} == {
        "n": 3,
        "p50_ms": 2,
        "p99_ms": 3,
        "max_ms": 3,
        "lost": 5,
    }
    assert output[3]["rx"] == {"Chunk": 5, "Radius": 7}
    assert output[3]["tx"] == {"Cache": 1, "Input": 16, "Request": 2}

    # Later independent calls see new captures; decreases reset the delta baseline.
    first.bot.tx[144], second.bot.tx[144] = 14, 2
    first.bot.rx[70], second.bot.rx[70] = 4, 1
    first.bot.lost, second.bot.lost = 0, 1
    fleet.stat()
    fleet.pkt()
    fleet.rtt()
    output = rows(stream)
    assert output[-3]["input_packets"] == {"b00": 14, "b01": 2}
    assert output[-3]["tick_lateness_us_max"] == 0
    assert output[-2]["rx"] == output[-2]["tx"] == {}
    assert output[-1]["lost"] == -4 and output[-1]["n"] == 0 and output[-1]["p50_ms"] is None
    first.bot.tx[144], second.bot.tx[144] = 15, 3
    first.bot.rx[70] = 5
    fleet.pkt()
    assert rows(stream)[-1]["rx"] == {"Radius": 1}
    assert rows(stream)[-1]["tx"] == {"Input": 2}
    assert retained.play_packet_counts_in[70] == 2 and retained.play_packet_counts_out[144] == 11


def test_chunk_views_preserve_negative_floor_disc_dimension_and_first_seen_order():
    tracker = ChunkTracker()
    tracker.start(0)
    for key, value in (
        ((1, -1, -2), 10.9),
        ((1, -2, -2), 3.2),
        ((0, -1, -2), 7),
        ((1, 0, -1), 8),
        ((1, -1, -1), 12.1),
        ((1, 0, -2), 14.8),
    ):
        tracker.add(key, value)
    geometry = (1, -0.01, -16.01, 0)
    assert tracker.view_count(*geometry) == 4
    assert tracker.deltas(*geometry) == [10, 0, 2, 2]
    assert not in_view((1, 0, -1), *geometry)
    assert tracker.view_count(0, -0.01, -16.01, 0) == 1
    assert tracker.view_count(1, 0, -16, 0) == 3
    tracker.add((1, -1, -3), 16.4)

    attempt = Attempt(
        0,
        "b00",
        Walker("none", 0),
        0,
        bot=StatisticsBot({}, {}, 0),
        online=True,
        position=(-0.01, 64, -16.01),
        dimension=1,
        radius=16,
        tracker=tracker,
    )
    assert attempt.row()[2] == 5
    assert attempt.summary() == {
        "bot": "b00",
        "uniq": 7,
        "in_view": 5,
        "chunks": 7,
        "corrections": 0,
        "first_seen_ms": [10, 0, 2, 2, 2],
    }
    attempt.online = False
    attempt.position = (0, 64, -16)
    attempt.radius = 0
    assert attempt.view_count() == 3
