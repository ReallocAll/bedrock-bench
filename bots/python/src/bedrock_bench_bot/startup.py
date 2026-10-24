# SPDX-License-Identifier: Apache-2.0
"""The protocol2193/no-download Go login boundary and its compact deferred handlers."""

from __future__ import annotations

from collections import Counter


class StartupOverflow(RuntimeError):
    pass


class Startup:
    MAX_RECORDS = 4096
    MAX_BLOBS = 65536

    def __init__(self):
        self.expected = {143, 2}  # NetworkSettings or a login rejection PlayStatus.
        self.pending_rx = Counter()
        self.records: list[tuple[int, tuple]] = []
        self.blobs = 0
        self.baseline: tuple[tuple[float, float, float], int, int] | None = None
        self.radius: int | None = None
        self.player_spawn = False
        self.failure = ""
        self.no_retry = False

    @property
    def complete(self):
        return self.baseline is not None and self.radius is not None and self.player_spawn

    def discard(self):
        self.pending_rx.clear()
        self.records.clear()
        self.blobs = 0
        self.baseline = None

    def fail(self, reason: str, *, no_retry=False):
        self.failure, self.no_retry = reason, no_retry
        self.discard()

    def check(self, *, records=1, blobs=0):
        if len(self.records) + records > self.MAX_RECORDS or self.blobs + blobs > self.MAX_BLOBS:
            raise StartupOverflow("startup deferred workload exceeds 4096 records or 65536 blob IDs")

    def error(self, packet_id: int, reason: str, recoverable: bool):
        self.check()
        self.records.append((-1, (packet_id, reason, recoverable)))

    def observe(self, decoded):
        if self.failure:
            return
        packet_id, packet = decoded.header.packet_id, decoded.payload
        if packet_id == 5:  # Go always consumes Disconnect internally, never as an application RX packet.
            return
        if packet_id in self.expected:
            if packet_id == 143:
                self.expected = {3, 2}
            elif packet_id == 2:
                if int(packet.status) == 0:  # LOGIN_SUCCESS
                    self.expected = {6}
                elif int(packet.status) == 3:  # PLAYER_SPAWN
                    self.player_spawn = True
            elif packet_id == 6:
                self.expected = {7}
            elif packet_id == 7:
                self.expected = {180, 11}
            elif packet_id == 11:
                self.baseline = (
                    self.position(packet.pos),
                    int(packet.runtime_id),
                    int(packet.settings.spawn_settings.dimension),
                )
                self.expected = {162}
            elif packet_id == 162:
                self.expected = {70, 2}
            elif packet_id == 70:
                self.radius = int(packet.chunk_radius)
                self.expected = {2}
            # Handshake and DimensionData retain the current expected IDs.
            return
        self.pending_rx[packet_id] += 1
        self.defer(packet_id, packet)

    @staticmethod
    def position(value):
        return float(value.x), float(value.y), float(value.z)

    def defer(self, packet_id, packet):
        # Check before copying lists/positions. No payload or DecodedPacket survives this callback.
        if packet_id == 58:
            metadata = packet.cache_metadata
            count = 0 if metadata is None else len(metadata)
            self.check(blobs=count)
            blobs = () if metadata is None else tuple(int(entry.blob_id) for entry in metadata)
            self.records.append(
                (
                    58,
                    (
                        int(packet.dimension_id),
                        int(packet.pos.x),
                        int(packet.pos.z),
                        packet.client_request_sub_chunk_limit,
                        blobs,
                    ),
                )
            )
            self.blobs += count
        elif packet_id == 174:
            entries = packet.sub_chunk_data
            count = sum(entry.blob_id is not None for entry in entries)
            self.check(blobs=count)
            blobs = tuple(int(entry.blob_id) for entry in entries if entry.blob_id is not None)
            self.records.append(
                (
                    174,
                    (
                        int(packet.dimension_type),
                        int(packet.center_pos.x),
                        int(packet.center_pos.z),
                        len(entries),
                        blobs,
                    ),
                )
            )
            self.blobs += count
        elif packet_id in (19, 161, 61):
            self.check()
            position = self.position(packet.pos)
            if packet_id == 19:
                values = (int(packet.player_id), position, int(packet.reset_position) == 2)
            elif packet_id == 61:
                values = (int(packet.dimension_id), position)
            else:
                values = (position,)
            self.records.append((packet_id, values))
        elif packet_id == 70:
            self.check()
            self.records.append((70, (int(packet.chunk_radius),)))
        elif packet_id == 42:
            self.check()
            self.records.append((42, (None, float(packet.health))))
        elif packet_id == 29:
            count = sum(attribute.name == "minecraft:health" for attribute in packet.attribute_data)
            self.check(records=count)
            for attribute in packet.attribute_data:
                if attribute.name == "minecraft:health":
                    self.records.append((42, (int(packet.runtime_id), float(attribute.current_value))))
        elif packet_id == 27 and int(packet.event_id) == 3:  # ActorEvent.DEATH
            self.check()
            self.records.append((42, (int(packet.runtime_id), 0.0)))
        elif packet_id == 45 and int(packet.state) == 1:  # READY_TO_SPAWN
            self.check()
            self.records.append((45, ()))
        elif packet_id == 115:
            self.check()
            self.records.append((115, (int(packet.create_time), bool(packet.from_server))))
