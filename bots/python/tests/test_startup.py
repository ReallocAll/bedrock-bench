# SPDX-License-Identifier: Apache-2.0
"""Synthetic startup seam checks; native ownership and server behavior are separate integration checks."""

from __future__ import annotations

import asyncio
import json
from dataclasses import replace
from importlib.resources import files
from types import SimpleNamespace as NS

import pytest
from bedrock_bench_bot.startup import Startup
from bedrock_bot import (
    DecodeErrorEvent,
    PacketDecodedEvent,
    TickEvent,
    Vec3,
)
from fake_sdk import frame
from test_adapter import CONFIG, by_event, exercise


def send(bot, decoded):
    bot.emit(PacketDecodedEvent(decoded))


def column(x=0, blobs=(7,), limit=2):
    return frame(
        58,
        dimension_id=0,
        pos=NS(x=x, z=0),
        client_request_sub_chunk_limit=limit,
        cache_metadata=[NS(blob_id=value) for value in blobs],
    )


def test_unexpected_early_handshake_id_does_not_advance_expected_ids():
    startup = Startup()
    startup.observe(frame(11, pos=Vec3(), runtime_id=42, settings=NS(spawn_settings=NS(dimension=0))))
    assert startup.expected == {143, 2} and startup.baseline is None and startup.pending_rx == {11: 1}


@pytest.mark.parametrize("kind", ["records", "blobs"])
def test_overflow_fails_visibly_without_copy_partial_replay_or_retry(monkeypatch, kind):
    if kind == "records":
        monkeypatch.setattr(Startup, "MAX_RECORDS", 2)
    else:
        monkeypatch.setattr(Startup, "MAX_BLOBS", 2)

    class DoNotCopy:
        def __len__(self):
            return 3

        def __iter__(self):
            raise AssertionError("blob inventory copied before capacity check")

    def startup(bot):
        if kind == "records":
            for _ in range(3):
                send(bot, column(blobs=()))
        else:
            decoded = column(blobs=())
            decoded.payload.cache_metadata = DoNotCopy()
            send(bot, decoded)

    code, output, sdk, _ = asyncio.run(
        exercise(replace(CONFIG, join_retries=3), plans=[{"startup": startup, "play_rx": {}}])
    )
    assert code == 2 and len(sdk.instances[0].bots) == 1 and not sdk.instances[0].bots[0].misses
    failure = by_event(output, "join_failed")[0]
    assert failure["final"] and "startup deferred workload exceeds" in failure["err"]
    assert by_event(output, "stat")[-1]["rx_pkts"] == 0 and not by_event(output, "spawned")


@pytest.mark.parametrize("packet_id,recoverable", [(143, True), (-1, False)])
def test_expected_or_framing_failure_is_counted_and_closes_before_ready(packet_id, recoverable):
    def startup(bot):
        bot.emit(DecodeErrorEvent(packet_id, "fatal startup decode", recoverable))

    code, output, sdk, _ = asyncio.run(exercise(plans=[{"startup": startup, "play_rx": {}}]))
    assert code == 2 and not by_event(output, "spawned") and len(sdk.instances[0].bots) == 1
    event = by_event(output, "decode_error")[0]
    assert event["pkt_id"] == (None if packet_id < 0 else packet_id)
    assert by_event(output, "stat")[-1]["decode_errors"] == 1
    from jsonschema import Draft202012Validator

    schema = files("bedrock_bench").joinpath("schemas/bot-event.schema.json")
    Draft202012Validator(json.loads(schema.read_text(encoding="utf-8"))).validate(event)


def test_stat_uses_successful_per_bot_input_histograms_and_resets_tick_max():
    async def action(fleet, runtime):
        bot = runtime.bots[0]
        bot.emit(TickEvent(tick=1, lateness_us=170))
        bot.emit(TickEvent(tick=2, lateness_us=30))
        # Two scheduled inputs, but only one successfully sent input in the independent histogram.
        fleet.stat()
        fleet.stat()
        fleet.request_stop()

    code, output, _, _ = asyncio.run(exercise(plans=[{"sent_inputs": 1}], action=action))
    stats = by_event(output, "stat")
    assert code == 0 and stats[0]["input_packets"] == {"b00": 1}
    assert [stat["tick_lateness_us_max"] for stat in stats] == [170, 0, 0]
