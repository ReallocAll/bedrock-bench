# SPDX-License-Identifier: Apache-2.0
from __future__ import annotations

import asyncio
import math
import time
from collections import Counter
from dataclasses import dataclass, field

from .config import Config
from .events import Emitter
from .startup import Startup, StartupOverflow
from .workload import ChunkTracker, Walker, bot_uuid, float32, in_view, round3, subchunk_heights


@dataclass(slots=True)
class Attempt:
    index: int
    name: str
    walker: Walker
    started_ms: float
    bot: object = None
    stage: str = "dial"
    spawned: bool = False
    online: bool = False
    reason: str = ""
    position: tuple[float, float, float] = (0, 0, 0)
    dimension: int = 0
    radius: int = 16
    teleports: int = 0
    corrections: int = 0
    columns: int = 0
    subchunks: int = 0
    deaths: int = 0
    tracker: ChunkTracker = field(default_factory=ChunkTracker)
    first_chunk: bool = False
    dead: bool = False
    respawn_attempts: int = 0
    respawn_due: float = math.inf
    probe_due: float = math.inf
    runtime_id: int = 0
    startup: Startup = field(default_factory=Startup)
    startup_rx: Counter = field(default_factory=Counter)
    startup_tx: Counter = field(default_factory=Counter)

    def _view_geometry(self):
        if self.online:
            self.radius = self.bot.get_status().chunk_radius
        x, _, z = self.position
        return self.dimension, x, z, self.radius

    def view_count(self):
        return self.tracker.view_count(*self._view_geometry())

    def row(self):
        coordinates = [round3(value) for value in self.position] + [self.dimension] if self.teleports else [None] * 4
        return [self.name, len(self.tracker.seen), self.view_count(), *coordinates, self.corrections, self.teleports]

    def summary(self):
        first_seen_ms = self.tracker.deltas(*self._view_geometry())
        return dict(
            bot=self.name,
            uniq=len(self.tracker.seen),
            in_view=len(first_seen_ms),
            chunks=self.tracker.received,
            corrections=self.corrections,
            first_seen_ms=first_seen_ms,
        )


class Fleet:
    """One owner loop and one SDK Runtime; first-join policy belongs to the benchmark."""

    retry_delay = 3.0

    def __init__(self, config: Config, sdk, emitter: Emitter, build: dict, stderr, *, names=None):
        self.config, self.sdk, self.emitter, self.build, self.stderr = config, sdk, emitter, build, stderr
        self.names = names or {}
        self.stop = asyncio.Event()
        self.exit_code = 0
        self.joined = self.failed = self.dropped = self.kicked = self.decode_errors = 0
        self.attempts: list[Attempt] = []
        self.by_bot = {}
        self.last_spawned: dict[int, Attempt] = {}
        self.started_indices: list[int] = []
        self.tasks = []
        self.last_rx, self.last_tx = Counter(), Counter()
        self.rtts = []
        self.last_lost = 0
        self.decode_burst = 0
        self.decode_last_ms = 0.0
        self.tick_lateness_us_max = 0
        self.runtime = None
        self.closing = False
        emitter.on_error = self.request_stop

    def request_stop(self, code: int = 0):
        self.exit_code = max(self.exit_code, code)
        self.stop.set()

    def log(self, message: str):
        print(message, file=self.stderr, flush=True)

    async def sleep(self, delay: float) -> bool:
        try:
            async with asyncio.timeout(delay):
                await self.stop.wait()
        except TimeoutError:
            return True
        return False

    def _mask(self):
        return sum(
            self.sdk.event_mask(getattr(self.sdk.EventKind, name))
            for name in (
                "state",
                "tick",
                "position",
                "chunk",
                "death",
                "respawn",
                "dimension",
                "latency",
                "decode_error",
            )
        )

    def _options(self, index: int):
        config = self.config
        timeout = math.ceil(config.spawn_timeout * 1000)
        options = dict(
            server_url=("http://" if config.transport == "nethernet" else "raknet://") + config.addr,
            transport=config.transport,
            name=config.name(index),
            uuid=bot_uuid(config.identity_seed, config.name(index)),
            protocol=2193,
            chunk_radius=config.radius,
            client_cache=True,
            client_side_generation=config.csg,
            connect_timeout_ms=timeout,
            spawn_timeout_ms=timeout,
            server_key_sha256=config.server_key_sha256.lower(),
            load_screen=config.loadscreen,
            validate_all_packets=True,
            recover_packet_errors=config.decode_errors == "count",
        )
        if config.fake_xuid:
            options["xuid"] = str(2535400000000000 + index)
        return self.sdk.BotOptions(**options)

    def resolve(self):
        if self.joined + self.failed == self.config.n:
            self.emitter.emit("alljoined", joined=self.joined, failed=self.failed)
            if self.joined == 0:
                self.request_stop(2)

    async def join(self, index: int):
        config = self.config
        for number in range(1, config.join_retries + 2):
            if self.stop.is_set():
                return
            attempt = Attempt(index, config.name(index), Walker(config.walk, index), self.emitter.now_ms())
            self.attempts.append(attempt)
            self.emitter.emit("dial", bot=attempt.name, i=index)
            bot = self.runtime.add_bot(self._options(index))
            attempt.bot = bot
            self.by_bot[bot] = attempt
            bot.set_event_interests(self.sdk.event_mask(self.sdk.EventKind.packet_decoded))
            try:
                # The SDK's connect/spawn timeouts are separate. This outer deadline includes both.
                async with asyncio.timeout(config.spawn_timeout):
                    await bot.wait_ready()
            except asyncio.CancelledError:
                raise
            except Exception as error:
                if attempt.spawned:
                    # Ready can be followed by a drop before wait_ready's postcondition runs.
                    # It is still a joined session and must never become a first-join retry.
                    return
                if self.stop.is_set():
                    return
                final = attempt.startup.no_retry or number > config.join_retries
                reason = attempt.startup.failure or str(error)
                attempt.startup.discard()
                self.emitter.emit(
                    "join_failed",
                    bot=attempt.name,
                    i=index,
                    stage=attempt.stage,
                    err=reason,
                    session=1,
                    attempt=number,
                    final=final,
                )
                await bot.disconnect("first join failed")
                if final:
                    self.failed += 1
                    self.resolve()
                    return
                if not await self.sleep(self.retry_delay):
                    return
                continue
            # Ready is recorded synchronously, before later packets can arrive.
            return  # A spawned connection is never reconnected automatically.

    async def launch(self):
        for index in range(self.config.n):
            if self.stop.is_set():
                return
            self.started_indices.append(index)
            task = asyncio.create_task(self.join(index), name=f"bench-join-{index}")
            self.tasks.append(task)
            task.add_done_callback(self._task_done)
            if index + 1 < self.config.n and not await self.sleep(self.config.stagger):
                return

    def _task_done(self, task):
        if not task.cancelled() and task.exception() is not None:
            self.log(f"benchmark task failed: {task.exception()}")
            self.request_stop(1)

    def event(self, bot, event):
        attempt = self.by_bot[bot]
        sdk = self.sdk
        if isinstance(event, sdk.StateEvent):
            self.state(attempt, event)
        elif isinstance(event, sdk.PacketDecodedEvent):
            if not attempt.spawned and not attempt.startup.failure:
                try:
                    attempt.startup.observe(event.packet)
                except StartupOverflow as error:
                    self.abort_startup(attempt, str(error), no_retry=True)
        elif isinstance(event, sdk.DecodeErrorEvent):
            if (
                not attempt.spawned
                and not attempt.startup.failure
                and event.recoverable
                and event.packet_id >= 0
                and event.packet_id != 5
                and event.packet_id not in attempt.startup.expected
            ):
                try:
                    attempt.startup.error(event.packet_id, event.reason, event.recoverable)
                except StartupOverflow as error:
                    self.abort_startup(attempt, str(error), no_retry=True)
            else:
                self.decode(attempt, event)
                if not attempt.spawned:
                    self.abort_startup(attempt, event.reason)
        elif attempt.spawned and attempt.online and not self.closing:
            self.play_event(attempt, event)

    def play_event(self, attempt, event):
        sdk = self.sdk
        if isinstance(event, sdk.TickEvent):
            self.tick_lateness_us_max = max(self.tick_lateness_us_max, max(0, int(event.lateness_us)))
            self.tick(attempt)
        elif isinstance(event, sdk.PositionEvent):
            attempt.position = (event.position.x, event.position.y, event.position.z)
            if event.correction:
                attempt.corrections += 1
            if event.teleport:
                attempt.teleports += 1
                attempt.tracker.start(self.emitter.now_ms())
                self.emitter.emit(
                    "tp",
                    bot=attempt.name,
                    pos=[round3(v) for v in attempt.position],
                    dim=attempt.dimension,
                    n=attempt.teleports,
                )
        elif isinstance(event, sdk.ChunkEvent):
            self.chunk(attempt, event)
        elif isinstance(event, sdk.DimensionEvent):
            attempt.dimension = event.dimension
            attempt.position = (event.position.x, event.position.y, event.position.z)
            self.emitter.emit("dim", bot=attempt.name, dim=event.dimension, session=1)
        elif isinstance(event, sdk.DeathEvent):
            if not attempt.dead:
                attempt.dead, attempt.respawn_attempts = True, 0
                attempt.deaths += 1
                attempt.respawn_due = time.monotonic() + 1
                self.emitter.emit("died", bot=attempt.name, session=1)
        elif isinstance(event, sdk.RespawnEvent):
            if attempt.dead and attempt.respawn_attempts:
                attempt.dead = False
                attempt.respawn_due = math.inf
                self.emitter.emit("respawned", bot=attempt.name, session=1)
        elif isinstance(event, sdk.LatencyEvent):
            self.rtts.append(event.round_trip_us / 1000)

    def abort_startup(self, attempt, reason, *, no_retry=False):
        if attempt.startup.failure:
            return
        attempt.startup.fail(reason, no_retry=no_retry)
        completion = attempt.bot.disconnect(reason)

        async def finish():
            await completion

        task = asyncio.create_task(finish(), name=f"bench-abort-{attempt.index}")
        self.tasks.append(task)
        task.add_done_callback(self._task_done)

    def replay(self, attempt, records):
        sdk = self.sdk
        for packet_id, values in records:
            if packet_id in (58, 174):
                dim, x, z, amount, blobs = values
                if packet_id == 58:
                    event = sdk.ChunkEvent(sdk.ChunkEvent.Kind.column, dim, x, z, subchunk_limit=amount, blob_ids=blobs)
                else:
                    event = sdk.ChunkEvent(sdk.ChunkEvent.Kind.subchunks, dim, x, z, entries=amount, blob_ids=blobs)
                self.chunk(attempt, event, refresh_radius=False)
            elif packet_id == 19:
                runtime_id, position, teleport = values
                if runtime_id == attempt.runtime_id:
                    self.play_event(attempt, sdk.PositionEvent(sdk.Vec3(*position), teleport=teleport))
            elif packet_id == 161:
                self.play_event(attempt, sdk.PositionEvent(sdk.Vec3(*values[0]), correction=True))
            elif packet_id == 61:
                self.play_event(attempt, sdk.DimensionEvent(values[0], sdk.Vec3(*values[1])))
            elif packet_id == 70:
                attempt.radius = values[0]
            elif packet_id == 42:
                runtime_id, health = values
                if runtime_id is None or runtime_id == attempt.runtime_id:
                    self.play_event(attempt, sdk.DeathEvent() if health <= 0 else sdk.RespawnEvent())
            elif packet_id == 45:
                self.play_event(attempt, sdk.RespawnEvent())
            elif packet_id == -1:
                self.decode(attempt, sdk.DecodeErrorEvent(*values))
            # The SDK already owns latency responses and dimension ACKs. Never send them twice.

    def state(self, attempt: Attempt, event):
        state = event.state
        states = self.sdk.BotState
        if state in (states.negotiating, states.logging_in):
            attempt.stage = "login"
        elif state == states.spawning:
            attempt.stage = "spawn"
        elif state == states.ready and not attempt.spawned:
            if attempt.startup.failure:
                return
            if not attempt.startup.complete:
                self.abort_startup(attempt, "SDK Ready before Go-compatible startup sequence completed")
                return
            attempt.position, attempt.runtime_id, attempt.dimension = attempt.startup.baseline
            attempt.radius = attempt.startup.radius
            attempt.startup_rx.update(attempt.startup.pending_rx)
            sent = attempt.bot.capture_statistics()
            login_only = {193, 1, 4, 129, 8, 113, 312}  # Initial loading-screen notifications belong to login.
            for packet_id, (total, play) in enumerate(
                zip(sent.packet_counts_out, sent.play_packet_counts_out, strict=False)
            ):
                count = total - play
                if packet_id == 69:
                    count = max(0, count - 1)  # The one successful initial16 request belongs to login.
                if count > 0 and packet_id not in login_only:
                    attempt.startup_tx[packet_id] = count
            records = attempt.startup.records
            attempt.startup.records = []
            attempt.startup.discard()
            attempt.bot.set_event_interests(0)
            attempt.spawned = attempt.online = True
            self.last_spawned[attempt.index] = attempt
            self.joined += 1
            if attempt.index < self.config.probe_bots:
                attempt.probe_due = time.monotonic() + 1
            self.emitter.emit(
                "spawned",
                bot=attempt.name,
                i=attempt.index,
                session=1,
                join_ms=round3(max(0, self.emitter.now_ms() - attempt.started_ms)),
                radius=attempt.radius,
                dim=attempt.dimension,
            )
            self.resolve()
            self.replay(attempt, records)
        elif state in (states.closed, states.failed):
            if not attempt.spawned:
                attempt.startup.discard()
            attempt.reason = event.reason
            if attempt.online:
                attempt.online = False
                if not self.closing and not self.stop.is_set():
                    self.dropped += 1
                    if event.disconnect is not None:
                        self.kicked += 1
                        self.emitter.emit("kicked", bot=attempt.name, session=1, reason=event.reason)
                    else:
                        self.emitter.emit("dropped", bot=attempt.name, session=1, err=event.reason)

    def tick(self, attempt: Attempt):
        status = attempt.bot.get_status()
        attempt.radius = status.chunk_radius
        now = time.monotonic()
        dx, dz = attempt.walker.step()
        x, y, z = attempt.position
        attempt.position = (x + dx, y, z + dz)
        attempt.bot.set_input(
            self.sdk.Input(
                position=self.sdk.Vec3(*(float32(v) for v in attempt.position)),
                delta=self.sdk.Vec3(float32(dx), 0, float32(dz)),
                move=self.sdk.Vec2(0, int(attempt.walker.moving)),
                yaw=float32(attempt.walker.degrees()),
                vertical_collision=True,
            )
        )
        if attempt.dead and now >= attempt.respawn_due and attempt.respawn_attempts < 4:
            if attempt.respawn_attempts:
                attempt.bot.respawn(retry=True)
            else:
                attempt.bot.respawn()
            attempt.respawn_attempts += 1
            attempt.respawn_due = now + 10
        if now >= attempt.probe_due:
            attempt.bot.probe_latency()
            attempt.probe_due += math.floor(now - attempt.probe_due) + 1

    def chunk(self, attempt: Attempt, event, *, refresh_radius=True):
        if refresh_radius:
            attempt.radius = attempt.bot.get_status().chunk_radius
        if event.blob_ids:
            attempt.bot.report_blob_status(event.blob_ids)
        if event.kind == self.sdk.ChunkEvent.Kind.column:
            attempt.columns += 1
            key = (event.dimension, event.x, event.z)
            now = self.emitter.now_ms()
            attempt.tracker.add(key, now)
            x, _, z = attempt.position
            if (
                attempt.tracker.origin is not None
                and not attempt.first_chunk
                and in_view(key, attempt.dimension, x, z, attempt.radius)
            ):
                attempt.first_chunk = True
                self.emitter.emit("first_chunk", bot=attempt.name, dt_ms=round3(max(0, now - attempt.tracker.origin)))
            if event.subchunk_limit is not None:
                attempt.bot.request_sub_chunks(
                    event.dimension, event.x, event.z, subchunk_heights(event.dimension, event.subchunk_limit)
                )
        else:
            attempt.subchunks += event.entries

    def decode(self, attempt: Attempt, event):
        self.decode_errors += 1
        now = self.emitter.now_ms()
        if self.decode_burst < 10 or now - self.decode_last_ms >= 1000:
            self.decode_burst += 1
            self.decode_last_ms = now
            self.emitter.emit(
                "decode_error",
                bot=attempt.name,
                pkt_id=None if event.packet_id < 0 else event.packet_id,
                err=event.reason,
                total=self.decode_errors,
            )
        if self.config.decode_errors == "fail":
            self.request_stop(3)

    def _report_counts(self):
        rx, tx, lost = Counter(), Counter(), 0
        input_counts = {}
        for attempt in self.attempts:
            if not attempt.spawned:
                continue
            stats = attempt.bot.capture_statistics()
            input_counts[id(attempt)] = stats.play_packet_counts_out[144]
            rx.update(attempt.startup_rx)
            tx.update(attempt.startup_tx)
            # Initial loading screens moved before SDK play; retain the Go frontend accounting window.
            tx[312] += stats.packet_counts_out[312] - stats.play_packet_counts_out[312]
            # Login-inclusive SDK counters deliberately never feed application packet statistics.
            for output, values in ((rx, stats.play_packet_counts_in), (tx, stats.play_packet_counts_out)):
                for packet_id, count in enumerate(values):
                    if count and not (output is rx and packet_id == 5):
                        output[packet_id] += count
            lost += stats.probes_lost
        inputs = {attempt.name: input_counts[id(attempt)] for _, attempt in sorted(self.last_spawned.items())}
        return rx, tx, lost, inputs

    def progress(self):
        rows = []
        for index in self.started_indices:
            attempt = self.last_spawned.get(index)
            rows.append(attempt.row() if attempt else [self.config.name(index), 0, 0, None, None, None, None, 0, 0])
        self.emitter.emit("progress", rows=rows)

    def stat(self, *, _counts=None):
        rx, tx, _, inputs = self._report_counts() if _counts is None else _counts
        self.emitter.emit(
            "stat",
            online=sum(a.online for a in self.attempts),
            joined=self.joined,
            failed=self.failed,
            dropped=self.dropped,
            left=0,
            rejoins=0,
            deaths=sum(a.deaths for a in self.attempts),
            rx_pkts=sum(rx.values()),
            tx_pkts=sum(tx.values()),
            rx_bytes=None,
            tx_bytes=None,
            corrections=sum(a.corrections for a in self.attempts),
            level_chunks=sum(a.columns for a in self.attempts),
            subchunks=sum(a.subchunks for a in self.attempts),
            uniq=sum(len(a.tracker.seen) for a in self.attempts),
            in_view=sum(a.view_count() for a in self.attempts if a.online),
            decode_errors=self.decode_errors,
            kicked=self.kicked,
            rejoin_failed=0,
            input_packets=inputs,
            tick_lateness_us_max=self.tick_lateness_us_max,
        )
        self.tick_lateness_us_max = 0

    def pkt(self, *, _counts=None):
        rx, tx, _, _ = self._report_counts() if _counts is None else _counts

        def delta(current, previous):
            result = Counter()
            for packet_id, count in current.items():
                if count > previous[packet_id]:
                    result[self.names.get(packet_id, f"id_{packet_id}")] += count - previous[packet_id]
            return dict(result)

        self.emitter.emit("pkt", rx=delta(rx, self.last_rx), tx=delta(tx, self.last_tx))
        self.last_rx, self.last_tx = rx, tx

    def rtt(self, *, _lost=None):
        lost = (
            sum(attempt.bot.capture_statistics().probes_lost for attempt in self.attempts if attempt.spawned)
            if _lost is None
            else _lost
        )
        samples, self.rtts = sorted(self.rtts), []
        values = [None] * 3
        if samples:
            values = [round3(samples[math.floor(q * (len(samples) - 1) + 0.5)]) for q in (0.5, 0.99, 1)]
        self.emitter.emit(
            "rtt", n=len(samples), p50_ms=values[0], p99_ms=values[1], max_ms=values[2], lost=lost - self.last_lost
        )
        self.last_lost = lost

    async def report(self):
        loop = asyncio.get_running_loop()
        intervals = {
            "progress": self.config.progress_interval,
            "stat": self.config.stat_interval,
            "pkt": self.config.pkt_interval,
        }
        deadlines = {name: loop.time() + interval for name, interval in intervals.items()}
        while not self.stop.is_set():
            if not await self.sleep(max(0, min(deadlines.values()) - loop.time())):
                return
            now = loop.time()
            counts = None
            for name, deadline in deadlines.items():
                if deadline <= now:
                    if name in ("stat", "pkt"):
                        if counts is None:
                            counts = self._report_counts()
                        getattr(self, name)(_counts=counts)
                    else:
                        getattr(self, name)()
                    if name == "stat" and self.config.probe_bots:
                        self.rtt(_lost=counts[2])
                    deadlines[name] += (math.floor((now - deadline) / intervals[name]) + 1) * intervals[name]

    async def run(self):
        # Validate SDK-specific options before starting any connection.
        self._options(0)
        self.emitter.emit("hello", build=self.build, flags=self.config.flags(), intervals_ms=self.config.intervals())
        async with self.sdk.Runtime(self.event, events=self._mask()) as runtime:
            self.runtime = runtime
            launch = asyncio.create_task(self.launch(), name="bench-launch")
            report = asyncio.create_task(self.report(), name="bench-report")
            closed = asyncio.create_task(runtime.wait_closed(), name="bench-runtime-closed")
            stopped = asyncio.create_task(self.stop.wait(), name="bench-stop")
            for task in (launch, report):
                task.add_done_callback(self._task_done)
            try:
                done, _ = await asyncio.wait((closed, stopped), return_when=asyncio.FIRST_COMPLETED)
                if closed in done:
                    closed.result()
                    self.request_stop(1)
                # Emit while live sessions still contribute to online and the current view.
                self.progress()
                counts = self._report_counts()
                self.stat(_counts=counts)
                self.pkt(_counts=counts)
                if self.config.probe_bots:
                    self.rtt(_lost=counts[2])
            finally:
                self.closing = True
                self.request_stop()
                for task in (launch, report, stopped, *self.tasks):
                    task.cancel()
                await asyncio.gather(launch, report, stopped, *self.tasks, return_exceptions=True)
                await runtime.aclose()
                await closed
            for _, attempt in sorted(self.last_spawned.items()):
                self.emitter.emit("bot_summary", **attempt.summary())
            self.emitter.emit("done")
        return self.exit_code
