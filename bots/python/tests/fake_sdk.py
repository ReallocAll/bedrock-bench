# SPDX-License-Identifier: Apache-2.0
"""Synthetic public SDK double. It does not establish transport or native protocol correctness."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import bedrock_bot as api
from bedrock_bot import (
    BotState,
    EventKind,
    PacketDecodedEvent,
    RespawnEvent,
    StateEvent,
    TickEvent,
    Vec3,
    event_mask,
)


def frame(packet_id, **payload):
    return SimpleNamespace(header=SimpleNamespace(packet_id=packet_id), payload=SimpleNamespace(**payload))


class FakeBot:
    def __init__(self, runtime, options, plan):
        self.runtime, self.options, self.plan = runtime, options, plan
        self.status = SimpleNamespace(
            state=BotState.scheduled, position=Vec3(0, 65, 0), chunk_radius=16, dimension=0, alive=True
        )
        self.ready = asyncio.get_running_loop().create_future()
        self.ready.add_done_callback(lambda future: None if future.cancelled() else future.exception())
        self.interests = 0
        self.inputs, self.misses = [], []
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
        self.emit(TickEvent(1, 0))

    def report_blob_status(self, missing, present=()):
        self.misses.append((tuple(missing), tuple(present)))

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
        tx = [0] * 1024
        tx[144] = self.plan.get("sent_inputs", len(self.inputs))
        all_tx = list(tx)
        all_tx[1], all_tx[69] = 20000, 1
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

    sdk = SimpleNamespace(**vars(api))
    sdk.Runtime, sdk.instances = Runtime, instances
    return sdk
