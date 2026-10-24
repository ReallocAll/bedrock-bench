# SPDX-License-Identifier: Apache-2.0
"""Synthetic public SDK double. It does not establish transport or native protocol correctness."""

from __future__ import annotations

import asyncio
from collections import Counter
from dataclasses import dataclass, field
from enum import IntEnum
from types import SimpleNamespace


class BotState(IntEnum):
    scheduled, connecting, negotiating, logging_in, spawning, ready, closing, closed, failed = range(9)


class EventKind(IntEnum):
    state, tick, position, chunk, death, respawn, dimension, latency, decode_error, packet_decoded = range(10)


@dataclass
class Vec2:
    x: float = 0
    y: float = 0


@dataclass
class Vec3:
    x: float = 0
    y: float = 0
    z: float = 0


@dataclass
class Input:
    position: Vec3 = field(default_factory=Vec3)
    delta: Vec3 = field(default_factory=Vec3)
    move: Vec2 = field(default_factory=Vec2)
    yaw: float = 0
    vertical_collision: bool = True


@dataclass
class BotOptions:
    server_url: str = ""
    name: str = ""
    uuid: str = ""
    protocol: int = 2193
    chunk_radius: int = 16
    client_cache: bool = True
    client_side_generation: bool = False
    connect_timeout_ms: int = 60000
    spawn_timeout_ms: int = 60000
    server_key_sha256: str = ""
    load_screen: bool = True
    validate_all_packets: bool = False
    recover_packet_errors: bool = False


@dataclass
class StateEvent:
    state: BotState
    reason: str = ""
    disconnect: object = None


@dataclass
class TickEvent:
    tick: int = 1
    lateness_us: int = 0


@dataclass
class PositionEvent:
    position: Vec3
    teleport: bool = False
    correction: bool = False


@dataclass
class ChunkEvent:
    class Kind(IntEnum):
        column = 0
        subchunks = 1

    kind: Kind
    dimension: int
    x: int
    z: int
    entries: int = 0
    subchunk_limit: int | None = None
    blob_ids: tuple = ()


class DeathEvent:
    pass


class RespawnEvent:
    pass


@dataclass
class DimensionEvent:
    dimension: int
    position: Vec3


@dataclass
class LatencyEvent:
    round_trip_us: int


@dataclass
class DecodeErrorEvent:
    packet_id: int
    reason: str
    recoverable: bool


@dataclass
class PacketDecodedEvent:
    packet: object


def frame(packet_id, **payload):
    return SimpleNamespace(header=SimpleNamespace(packet_id=packet_id), payload=SimpleNamespace(**payload))


def event_mask(kind):
    return 1 << kind


class FakeBot:
    def __init__(self, runtime, options, plan):
        self.runtime, self.options, self.plan = runtime, options, plan
        self.status = SimpleNamespace(
            state=BotState.scheduled, position=Vec3(0, 65, 0), chunk_radius=16, dimension=0, alive=True
        )
        self.ready = asyncio.get_running_loop().create_future()
        self.ready.add_done_callback(lambda future: None if future.cancelled() else future.exception())
        self.interests = 0
        self.interest_history = []
        self.play_rx = Counter(plan.get("play_rx", {58: 3}))
        self.inputs, self.consumed, self.misses, self.subrequests, self.radii = [], [], [], [], []
        self.respawns = self.probes = 0
        self.respawn_calls = []
        self.respawn_pending = False
        self.task = asyncio.create_task(self.drive())

    def emit(self, event):
        if isinstance(event, PacketDecodedEvent) and not self.interests & event_mask(EventKind.packet_decoded):
            return
        if isinstance(event, StateEvent):
            self.status.state = event.state
        if isinstance(event, RespawnEvent):
            self.respawn_pending = False
        self.runtime.callback(self, event)

    def set_event_interests(self, interests):
        self.interests = interests
        self.interest_history.append(interests)

    def receive(self, packet_id, projection=None, **payload):
        if self.status.state == BotState.ready:
            self.play_rx[packet_id] += 1
        self.emit(PacketDecodedEvent(frame(packet_id, **payload)))
        if projection is not None:
            self.emit(projection)

    def login_frames(self):
        return [
            frame(143),
            frame(2, status=0),
            frame(6),
            frame(7),
            frame(
                11,
                pos=Vec3(0, 65, 0),
                runtime_id=42,
                settings=SimpleNamespace(spawn_settings=SimpleNamespace(dimension=0)),
            ),
            frame(162),
            frame(70, chunk_radius=16),
            frame(2, status=3),
        ]

    async def drive(self):
        self.emit(StateEvent(BotState.connecting))
        if self.plan.get("hang"):
            await asyncio.Future()
        self.emit(StateEvent(BotState.negotiating))
        self.emit(StateEvent(BotState.spawning))
        if self.plan.get("fail"):
            self.emit(StateEvent(BotState.failed, "synthetic join failure"))
            self.ready.set_exception(RuntimeError("synthetic join failure"))
            return
        if "startup" in self.plan:
            self.plan["startup"](self)
        else:
            for packet in self.login_frames():
                self.emit(PacketDecodedEvent(packet))
        if self.status.state in (BotState.closing, BotState.failed):
            return
        self.emit(StateEvent(BotState.ready))
        if self.plan.get("ready_then_fail"):
            self.emit(StateEvent(BotState.failed, "synthetic immediate drop"))
            self.ready.set_exception(RuntimeError("session is closed or invalidated"))
            return
        self.ready.set_result(None)
        await asyncio.sleep(0)
        if "after" in self.plan:
            self.plan["after"](self)

    async def wait_ready(self):
        await asyncio.shield(self.ready)
        if self.status.state != BotState.ready:
            raise RuntimeError("session is closed or invalidated")

    def disconnect(self, reason=""):
        self.status.state = BotState.closing

        async def close():
            if not self.task.done():
                self.task.cancel()
            await asyncio.gather(self.task, return_exceptions=True)
            if not self.ready.done():
                self.ready.set_exception(RuntimeError(reason or "closed"))
            self.emit(StateEvent(BotState.closed, reason))

        return close()

    def get_status(self):
        return self.status

    def set_input(self, frame):
        self.inputs.append(frame)

    def tick(self):
        self.emit(TickEvent())
        self.consumed.append(self.inputs[-1])  # SDK consumes input after TickEvent returns.

    def report_blob_status(self, missing, present=()):
        self.misses.append((tuple(missing), tuple(present)))

    def request_sub_chunks(self, dim, x, z, heights):
        self.subrequests.append((dim, x, z, list(heights)))

    def request_chunk_radius(self, radius):
        self.radii.append(radius)

    def respawn(self, *, retry=False):
        self.respawn_calls.append(retry)
        if self.respawn_pending and not retry:
            return
        self.respawn_pending = True
        self.respawns += 1

    def probe_latency(self):
        self.probes += 1

    def capture_statistics(self):
        rx = [0] * 1024
        for packet_id, count in self.play_rx.items():
            rx[packet_id] = count
        tx = [0] * 1024
        for packet_id, count in self.plan.get("play_tx", {}).items():
            tx[packet_id] = count
        tx[144] = self.plan.get("sent_inputs", len(self.inputs))
        all_tx = list(tx)
        for packet_id, count in self.plan.get("preplay_tx", {1: 20000, 69: 1}).items():
            all_tx[packet_id] += count
        return SimpleNamespace(
            packet_counts_in=[10000] * 256,
            packet_counts_out=all_tx,
            packets_in=2560000,
            packets_out=sum(all_tx),
            play_packet_counts_in=rx,
            play_packet_counts_out=tx,
            probes_lost=2,
        )


def make_sdk(plans=None):
    plans = list(plans or [{}])
    instances = []

    class Runtime:
        def __init__(self, callback, *, events):
            self.callback, self.events = callback, events
            self.bots = []
            self.closed = asyncio.Event()
            self.cleanup_finished = False
            instances.append(self)

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_):
            await self.aclose()

        def add_bot(self, options):
            bot = FakeBot(self, options, plans.pop(0))
            self.bots.append(bot)
            return bot

        async def aclose(self):
            if not self.cleanup_finished:
                await asyncio.gather(*(bot.disconnect("runtime stopped") for bot in self.bots))
                self.cleanup_finished = True
                self.closed.set()

        async def wait_closed(self):
            await self.closed.wait()

    sdk = SimpleNamespace(
        **{
            name: value
            for name, value in globals().items()
            if name
            in (
                "BotOptions",
                "BotState",
                "EventKind",
                "Vec2",
                "Vec3",
                "Input",
                "StateEvent",
                "TickEvent",
                "PositionEvent",
                "ChunkEvent",
                "DeathEvent",
                "RespawnEvent",
                "DimensionEvent",
                "LatencyEvent",
                "DecodeErrorEvent",
                "PacketDecodedEvent",
                "event_mask",
            )
        }
    )
    sdk.Runtime, sdk.instances = Runtime, instances
    return sdk
