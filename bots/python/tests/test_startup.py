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
from fake_sdk import (
    BotState,
    ChunkEvent,
    DecodeErrorEvent,
    PacketDecodedEvent,
    StateEvent,
    TickEvent,
    Vec3,
    frame,
)
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


def subchunks(blobs=(8, 8)):
    return frame(174, dimension_type=0, center_pos=NS(x=0, z=0), sub_chunk_data=[NS(blob_id=value) for value in blobs])


def custom_startup(bot, before=(), after=()):
    login = bot.login_frames()
    for decoded in login[:4]:
        send(bot, decoded)
    for decoded in before:
        send(bot, decoded)
    for decoded in login[4:7]:
        send(bot, decoded)
    for decoded in after:
        send(bot, decoded)
    send(bot, login[7])


async def stop(fleet, runtime):
    fleet.request_stop()


def test_pre_ready_column_requests_and_rx_are_promoted_once_without_early_projection_duplication():
    def startup(bot):
        custom_startup(bot, before=[column()])
        bot.emit(ChunkEvent(ChunkEvent.Kind.column, 0, 0, 0, subchunk_limit=2, blob_ids=(7,)))
        assert bot.misses == bot.subrequests == []

    code, output, sdk, fleet = asyncio.run(exercise(plans=[{"startup": startup, "play_rx": {}}], action=stop))
    bot = sdk.instances[0].bots[0]
    assert code == 0 and bot.misses == [((7,), ())] and bot.subrequests == [(0, 0, 0, [-4, -3])]
    assert by_event(output, "stat")[-1]["rx_pkts"] == 1
    assert by_event(output, "stat")[-1]["level_chunks"] == 1
    assert by_event(output, "stat")[-1]["uniq"] == 0
    assert by_event(output, "pkt")[-1]["rx"] == {"LevelChunk": 1}
    assert bot.interest_history[-1] == 0
    assert not fleet.attempts[0].startup.records and not fleet.attempts[0].startup.pending_rx


def test_duplicate_subchunks_and_chunks_keep_order_and_blob_duplicates():
    def startup(bot):
        custom_startup(bot, before=[column(blobs=(7, 7)), subchunks(), column(blobs=(9,))])
        assert not bot.misses

    code, output, sdk, _ = asyncio.run(exercise(plans=[{"startup": startup, "play_rx": {}}], action=stop))
    bot = sdk.instances[0].bots[0]
    assert code == 0 and bot.misses == [((7, 7), ()), ((8, 8), ()), ((9,), ())]
    stat = by_event(output, "stat")[-1]
    assert (stat["rx_pkts"], stat["level_chunks"], stat["subchunks"]) == (3, 2, 2)


def test_classifier_uses_expected_state_for_radius_dimension_data_and_unknown_disconnect():
    def startup(bot):
        custom_startup(bot, before=[frame(180)], after=[frame(180), frame(70, chunk_radius=8), frame(900), frame(5)])
        bot.status.chunk_radius = 8  # The SDK has already processed the duplicate radius by Ready.

    code, output, _, fleet = asyncio.run(exercise(plans=[{"startup": startup, "play_rx": {5: 1}}], action=stop))
    assert code == 0 and by_event(output, "spawned")[0]["radius"] == 16
    assert fleet.attempts[0].startup_rx == {180: 1, 70: 1, 900: 1}
    assert by_event(output, "stat")[-1]["rx_pkts"] == 3  # Both startup and play Disconnect are excluded.


def test_unexpected_early_handshake_id_does_not_advance_expected_ids():
    startup = Startup()
    startup.observe(frame(11, pos=Vec3(), runtime_id=42, settings=NS(spawn_settings=NS(dimension=0))))
    assert startup.expected == {143, 2} and startup.baseline is None and startup.pending_rx == {11: 1}


def test_start_game_baseline_and_targeted_early_movement_replay_before_first_input():
    def startup(bot):
        custom_startup(
            bot,
            before=[
                frame(19, player_id=999, pos=Vec3(1000, 1, 1000), reset_position=2),
                frame(19, player_id=42, pos=Vec3(64, 70, 64), reset_position=2),
                column(x=4),
            ],
            after=[frame(161, pos=Vec3(80, 72, 80))],
        )
        bot.status.position = Vec3(900, 999, 900)  # Must not replace StartGame baseline or replayed movement.

    async def action(fleet, runtime):
        bot = runtime.bots[0]
        assert fleet.attempts[0].position == (80, 72, 80)
        bot.tick()
        fleet.request_stop()

    code, output, sdk, _ = asyncio.run(exercise(plans=[{"startup": startup, "play_rx": {}}], action=action))
    assert code == 0 and len(by_event(output, "tp")) == 1
    assert by_event(output, "tp")[0]["pos"] == [64, 70, 64]
    assert by_event(output, "stat")[-1]["corrections"] == 1
    assert by_event(output, "stat")[-1]["uniq"] == 1 and len(by_event(output, "first_chunk")) == 1
    assert sdk.instances[0].bots[0].inputs[0].position.y == 72


def test_final_spawn_then_application_same_batch_is_only_sdk_play_count():
    def startup(bot):
        custom_startup(bot, after=[column()])

    def after(bot):
        bot.receive(
            58,
            projection=ChunkEvent(ChunkEvent.Kind.column, 0, 1, 0, blob_ids=(99,)),
            dimension_id=0,
            pos=NS(x=1, z=0),
            client_request_sub_chunk_limit=None,
            cache_metadata=[NS(blob_id=99)],
        )

    async def action(fleet, runtime):
        while len(runtime.bots[0].misses) < 2:
            await asyncio.sleep(0)
        fleet.request_stop()

    code, output, sdk, _ = asyncio.run(
        exercise(plans=[{"startup": startup, "after": after, "play_rx": {}}], action=action)
    )
    assert code == 0 and by_event(output, "stat")[-1]["rx_pkts"] == 2
    assert by_event(output, "stat")[-1]["level_chunks"] == 2
    assert sdk.instances[0].bots[0].misses == [((7,), ()), ((99,), ())]


def test_deferred_bad_body_then_valid_frame_errors_once_and_never_adds_failed_rx():
    def startup(bot):
        for decoded in bot.login_frames()[:4]:
            send(bot, decoded)
        bot.emit(DecodeErrorEvent(174, "bad subchunk body", True))
        assert not bot.misses
        send(bot, subchunks((8,)))
        send(bot, column())
        for decoded in bot.login_frames()[4:]:
            send(bot, decoded)

    code, output, sdk, _ = asyncio.run(exercise(plans=[{"startup": startup, "play_rx": {}}], action=stop))
    assert code == 0 and len(by_event(output, "decode_error")) == 1
    stat = by_event(output, "stat")[-1]
    assert stat["decode_errors"] == 1 and stat["rx_pkts"] == 2
    assert sdk.instances[0].bots[0].misses == [((8,), ()), ((7,), ())]


def test_failed_startup_discards_pending_rx_errors_and_workload():
    def startup(bot):
        send(bot, column())
        send(bot, frame(900))
        bot.emit(DecodeErrorEvent(174, "deferred body error", True))
        bot.emit(StateEvent(BotState.failed, "startup failed"))
        bot.ready.set_exception(RuntimeError("startup failed"))

    code, output, sdk, fleet = asyncio.run(exercise(plans=[{"startup": startup, "play_rx": {58: 99}}]))
    assert code == 2 and by_event(output, "stat")[-1]["rx_pkts"] == 0
    assert not by_event(output, "decode_error") and not sdk.instances[0].bots[0].misses
    assert not fleet.attempts[0].startup.records and not fleet.attempts[0].startup.pending_rx


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


@pytest.mark.parametrize("latency_succeeded", [False, True])
def test_startup_tx_seeds_successful_sends_once_and_excludes_login(latency_succeeded):
    before = {193: 1, 1: 1, 4: 1, 129: 1, 8: 2, 113: 1, 69: 1}
    if latency_succeeded:
        before[115] = 1
    plan = {"preplay_tx": before, "play_tx": {69: 1, 312: 2}, "sent_inputs": 3}
    code, output, _, fleet = asyncio.run(exercise(plans=[plan], action=stop))
    assert code == 0 and fleet.attempts[0].startup_tx == ({115: 1} if latency_succeeded else {})
    stat = by_event(output, "stat")[-1]
    assert stat["tx_pkts"] == 6 + int(latency_succeeded)
    tx = by_event(output, "pkt")[-1]["tx"]
    assert tx == {"id_69": 1, "id_312": 2, "PlayerAuthInput": 3, **({"id_115": 1} if latency_succeeded else {})}


def test_failed_startup_does_not_seed_successful_preinit_sends():
    code, output, _, fleet = asyncio.run(exercise(plans=[{"fail": True, "preplay_tx": {115: 1, 69: 1}}]))
    assert code == 2 and by_event(output, "stat")[-1]["tx_pkts"] == 0
    assert not fleet.attempts[0].startup_tx
