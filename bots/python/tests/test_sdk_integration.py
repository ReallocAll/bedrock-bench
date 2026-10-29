# SPDX-License-Identifier: Apache-2.0
"""Installed SDK/native integration over controlled in-memory transport, not NetherNet/BDS.

Pass --sdk-source with the extracted SDK sdist (or a local SDK checkout for focused tests).
The SDK, adapter and native codec must be installed; no source import fallback is used.
"""

from __future__ import annotations

import asyncio
import io
import json
import sys
from dataclasses import replace
from pathlib import Path
from uuid import UUID

import endpilot as api
import bedrock_protocol as bp
import pytest
from bedrock_bench_bot.config import Config
from bedrock_bench_bot.events import Emitter
from bedrock_bench_bot.fleet import Fleet
from bedrock_bench_bot.identity import load_sdk, packet_names

CONFIG = Config(addr="controlled:19132", server_key_sha256="ab" * 32, stagger=0, spawn_timeout=2)
BUILD = {"module": "python", "version": "0.1.0.dev0", "protocol": 2193}


def run(coroutine):
    if sys.platform == "win32":
        return asyncio.run(coroutine, loop_factory=asyncio.SelectorEventLoop)
    return asyncio.run(coroutine)


def uvarint(value):
    result = bytearray()
    while value > 127:
        result.append((value & 127) | 128)
        value >>= 7
    result.append(value)
    return bytes(result)


def column(p, blob, *, limit=None):
    return p.LevelChunkPacket(
        pos=p.ChunkPos(x=4, z=4),
        dimension_id=0,
        sub_chunks_count=0xFFFFFFFE if limit is not None else 0,
        client_request_sub_chunk_limit=limit,
        cache_enabled=True,
        cache_metadata=[p.LevelChunkPacket.SubChunkMetadata(blob_id=blob)],
        serialized_chunk=b"",
    )


def subchunk(p):
    heights = p.SubChunkPacket.HeightmapData(
        height_map_type=p.SubChunkPacket.HeightMapDataType.NO_DATA,
        subchunk_height_map=None,
        render_height_map_type=p.SubChunkPacket.HeightMapDataType.NO_DATA,
        subchunk_render_height_map=None,
    )
    entries = [
        p.SubChunkPacket.SubChunkPacketData(
            sub_chunk_pos_offset=p.SubChunkPacket.SubChunkPosOffset(x=0, y=y, z=0),
            result=p.SubChunkPacket.SubChunkRequestResult.SUCCESS,
            serialized_sub_chunk=b"",
            height_map_data=heights,
            blob_id=12,
        )
        for y in (-4, -3)
    ]
    return p.SubChunkPacket(
        cache_enabled=True, dimension_type=0, center_pos=p.SubChunkPos(x=4, y=0, z=4), sub_chunk_data=entries
    )


def teleport(p, runtime_id):
    return p.MovePlayerPacket(
        player_id=runtime_id,
        pos=p.Vec3(x=64, y=70, z=64),
        rot=p.Vec2(x=0, y=0),
        y_head_rot=0,
        reset_position=p.PlayerPositionModeComponent.PositionMode.TELEPORT,
        on_ground=True,
        riding_id=0,
        teleport_data=p.MovePlayerTeleportData(cause=0, source_entity_type=0),
        tick=0,
    )


def death_packets(p, runtime_id, mode):
    if mode == "early_actor_death":
        packets = [
            p.ActorEventPacket(runtime_id=runtime_id, event_id=p.ActorEvent.DEATH, data=0, fire_at_position=None)
        ]
    elif mode == "early_attribute_death":
        packets = [
            p.UpdateAttributesPacket(
                runtime_id=runtime_id,
                tick=1,
                attribute_data=[
                    p.AttributeData(
                        name="minecraft:health",
                        min_value=0,
                        max_value=20,
                        current_value=value,
                        default_min_value=0,
                        default_max_value=20,
                        default_value=20,
                        modifiers=[],
                    )
                    for value in (5, 0)
                ],
            )
        ]
    else:
        packets = [p.SetHealthPacket(health=0)]
    return packets + [p.SetHealthPacket(health=20)]


def spawn_sequence(peer, mode, fixture_root):
    p = peer.p
    game = peer.codec.decode_packet((fixture_root / "start_game_2193.bin").read_bytes()).payload
    packets = [
        p.PlayStatusPacket(status=p.PlayStatus.LOGIN_SUCCESS),
        p.ResourcePacksInfoPacket(
            resource_pack_required=False,
            has_addon_packs=False,
            has_scripts=False,
            force_disable_vibrant_visuals=False,
            world_template_id_and_version=p.PackIdVersion(id=UUID(int=0), version=p.SemVersion(version="0.0.0")),
            resource_packs=[],
        ),
        p.ResourcePackStackPacket(
            texture_pack_required=False,
            texture_pack_ids_and_versions=[],
            base_game_version="1.26.50",
            experiments=p.Experiments(toggles=[], experiments_ever_toggled=False),
            include_editor_packs=False,
        ),
        p.DimensionDataPacket(dimension_definitions={}),
    ]
    mixed = mode in ("mixed", "malformed", "failed_send", "strict")
    if mixed:
        packets.append(column(p, 11, limit=2))
    if mode == "early_teleport":
        packets.append(teleport(p, game.runtime_id))
    if mode in ("early_actor_death", "early_attribute_death", "early_set_health_death"):
        packets.extend(death_packets(p, game.runtime_id, mode))
    packets += [game, p.ItemRegistryPacket(items=[])]
    if mixed:
        packets.append(teleport(p, game.runtime_id))
        packets.append(subchunk(p))
    packets.append(p.ChunkRadiusUpdatedPacket(chunk_radius=16))
    if mixed:
        packets += [
            p.ChunkRadiusUpdatedPacket(chunk_radius=8),
            p.DimensionDataPacket(dimension_definitions={}),
            bp.UnknownPacket(packet_id=900, body=b"unknown"),
            p.NetworkStackLatencyPacket(create_time=123456, from_server=True),
        ]
    packets.append(p.PlayStatusPacket(status=p.PlayStatus.PLAYER_SPAWN))
    if mixed:
        packets.append(column(p, 22))
    if mode not in ("early_actor_death", "early_attribute_death", "early_set_health_death"):
        packets.append(p.SetHealthPacket(health=17))
    encoded = [peer.codec.encode_packet(packet) for packet in packets]
    if mode in ("malformed", "strict"):
        # Actual native framing/decoding, with one malformed unexpected SubChunk body before a valid one.
        index = next(index for index, packet in enumerate(packets) if packet.packet_id == 174)
        encoded.insert(index, uvarint(174))
    peer.incoming.put_nowait(b"\xff" + b"".join(uvarint(len(packet)) + packet for packet in encoded))
    if mode == "failed_send":
        peer.send_error = RuntimeError("controlled send rejection before startup latency response")


async def exercise(monkeypatch, controlled, mode="normal", *, kick=False):
    fixtures = Path(controlled.__file__).parent / "fixtures"
    monkeypatch.setattr(controlled.Peer, "spawn", lambda peer: spawn_sequence(peer, mode, fixtures))
    peers = controlled.Peers(monkeypatch, compression_threshold=65535)
    output, diagnostics = io.StringIO(), io.StringIO()
    sdk = load_sdk()
    config = replace(
        CONFIG, radius=8 if mode != "normal" else 16, decode_errors="fail" if mode == "strict" else "count"
    )
    fleet = Fleet(config, sdk, Emitter(output), BUILD, diagnostics, names=packet_names())
    observed = []
    original_event = fleet.event

    def callback(bot, event):
        if isinstance(event, api.PacketDecodedEvent):
            observed.append((event.packet.header.packet_id, type(event.packet.payload).__name__))
            with pytest.raises((TypeError, AttributeError)):
                event.packet.payload = None
        original_event(bot, event)

    fleet.event = callback
    task = asyncio.create_task(fleet.run())
    try:
        await controlled.until(lambda: fleet.joined or task.done(), timeout=4)
        if mode in ("strict", "failed_send"):
            result = await asyncio.wait_for(task, 4)
        else:
            assert fleet.joined == 1, output.getvalue() + diagnostics.getvalue()
            peer = peers.connections[0]
            await controlled.until(lambda: sum(packet.packet_id == 144 for packet in peer.received) >= 3)
            if mode in ("early_actor_death", "early_attribute_death", "early_set_health_death"):
                bot = fleet.attempts[0].bot
                assert fleet.attempts[0].dead and not bot.get_status().alive
                assert bot.get_status().health == 20 and bot.capture_statistics().deaths == 1
                # The adapter's ordinary public respawn() call must succeed after the one-second death delay.
                await controlled.until(lambda: any(packet.packet_id == 45 for packet in peer.received))
                assert fleet.attempts[0].respawn_attempts == 1
                p = peer.p
                peer.send(
                    p.RespawnPacket(
                        pos=p.Vec3(x=10, y=80, z=20),
                        state=p.PlayerRespawnState.READY_TO_SPAWN,
                        runtime_id=bot.get_status().actor_runtime_id,
                    ),
                    p.SetHealthPacket(health=20),
                )
                await controlled.until(lambda: bot.get_status().alive and not fleet.attempts[0].dead)
                assert bot.capture_statistics().respawns == 1
            if kick:
                p = peer.p
                peer.send(
                    p.DisconnectPacket(
                        reason=p.DisconnectFailReason.LEGACY_DISCONNECT,
                        messages=bp.Variant(
                            0, p.DisconnectPacketMessages(message="controlled server kick", filtered_message="filtered")
                        ),
                    )
                )
                await controlled.until(lambda: fleet.kicked == 1)
            fleet.request_stop()
            result = await asyncio.wait_for(task, 4)
    finally:
        if not task.done():
            fleet.request_stop()
            await asyncio.wait_for(task, 4)
    assert len(peers.connections) == 1 and peers.connections[0].closed
    peer = peers.connections[0]
    events = [json.loads(line) for line in output.getvalue().splitlines()]
    assert events[0]["ev"] == "hello" and events[-1]["ev"] == "done"
    return result, events, peers.connections[0], fleet, observed


@pytest.mark.parametrize("mode", ["normal", "mixed", "malformed"])
def test_installed_sdk_native_startup_and_inputs(monkeypatch, sdk_peer, mode):
    code, events, peer, fleet, observed = run(exercise(monkeypatch, sdk_peer, mode))
    assert code == 0
    stat = next(event for event in reversed(events) if event["ev"] == "stat")
    assert stat["joined"] == stat["online"] == 1 and stat["input_packets"]["b00"] >= 3
    assert stat["decode_errors"] == int(mode == "malformed")
    assert observed and observed[0][0] == 143
    assert not fleet.attempts[0].startup.records and not fleet.attempts[0].startup.pending_rx
    assert len([event for event in events if event["ev"] == "alljoined"]) == 1
    if mode == "normal":
        assert stat["rx_pkts"] == 1 and fleet.attempts[0].startup_tx == {}
    else:
        assert stat["rx_pkts"] == 9
        assert stat["level_chunks"] == stat["subchunks"] == 2
        assert stat["uniq"] == 1 and fleet.attempts[0].startup_tx == {115: 1}
        misses = [packet for packet in peer.received if packet.packet_id == 135]
        assert [list(packet.missing_ids) for packet in misses] == [[11], [12, 12], [22]]
        requests = [packet for packet in peer.received if packet.packet_id == 175]
        assert len(requests) == 1 and [offset.y for offset in requests[0].sub_chunk_pos_offsets] == [-4, -3]
        # Configured radius and loading packets are already SDK play sends, counted once.
        pkt = next(event for event in reversed(events) if event["ev"] == "pkt")
        assert pkt["tx"]["RequestChunkRadius"] == 1 and pkt["tx"]["ServerBoundLoadingScreen"] == 2
        assert pkt["tx"]["NetworkStackLatency"] == 1
        assert not any(packet_id == 5 for packet_id, _ in observed)  # Observation removed at Ready.


def test_installed_sdk_native_disconnect_text_and_excluded_rx(monkeypatch, sdk_peer):
    code, events, _, _, _ = run(exercise(monkeypatch, sdk_peer, kick=True))
    assert code == 0
    stat = next(event for event in reversed(events) if event["ev"] == "stat")
    assert stat["kicked"] == stat["dropped"] == 1 and stat["rx_pkts"] == 1
    assert next(event for event in events if event["ev"] == "kicked")["reason"] == "controlled server kick"


def test_installed_sdk_native_pre_start_game_self_teleport_has_one_ack(monkeypatch, sdk_peer):
    code, events, peer, _, observed = run(exercise(monkeypatch, sdk_peer, "early_teleport"))
    assert code == 0
    inputs = [packet for packet in peer.received if packet.packet_id == 144]
    flag = peer.p.PlayerAuthInputPacket.InputData.HANDLED_TELEPORT
    assert [flag in packet.input_data for packet in inputs] == [True] + [False] * (len(inputs) - 1)
    assert all(packet.pos.y == 70 for packet in inputs)
    assert sum(packet_id == 19 for packet_id, _ in observed) == 1
    assert len([event for event in events if event["ev"] == "tp"]) == 1


@pytest.mark.parametrize("mode", ["early_actor_death", "early_attribute_death", "early_set_health_death"])
def test_installed_sdk_native_pre_start_game_death_replays_once_and_public_respawn_succeeds(
    monkeypatch,
    sdk_peer,
    mode,
):
    code, events, peer, _, _ = run(exercise(monkeypatch, sdk_peer, mode))
    assert code == 0
    assert len([event for event in events if event["ev"] == "died"]) == 1
    assert len([event for event in events if event["ev"] == "respawned"]) == 1
    assert len([packet for packet in peer.received if packet.packet_id == 45]) == 1


@pytest.mark.parametrize("mode,exit_code", [("strict", 3), ("failed_send", 2)])
def test_installed_sdk_native_failed_attempt_promotes_no_application_counts(monkeypatch, sdk_peer, mode, exit_code):
    code, events, peer, fleet, _ = run(exercise(monkeypatch, sdk_peer, mode))
    assert code == exit_code
    stat = next(event for event in reversed(events) if event["ev"] == "stat")
    assert stat["joined"] == stat["rx_pkts"] == stat["tx_pkts"] == 0
    assert not fleet.attempts[0].startup_tx
    assert not any(packet.packet_id == 115 for packet in peer.received)
