# SPDX-License-Identifier: Apache-2.0
from __future__ import annotations

import math
import struct
from uuid import NAMESPACE_URL, UUID, uuid5


def bot_uuid(seed: str, name: str) -> str:
    try:
        namespace = UUID(seed) if len(seed) == 36 and str(UUID(seed)) == seed.lower() else uuid5(NAMESPACE_URL, seed)
    except ValueError:
        namespace = uuid5(NAMESPACE_URL, seed)
    return str(uuid5(namespace, name))


def float32(value: float) -> float:
    return struct.unpack("<f", struct.pack("<f", value))[0]


def round3(value: float) -> float:
    # Go math.Round rounds ties away from zero.
    return math.copysign(math.floor(abs(value) * 1000 + 0.5) / 1000, value)


class Walker:
    def __init__(self, mode: str, index: int):
        self.yaw = ((index * 137.508) % 360) * math.pi / 180
        self.moving = mode == "circle"

    def step(self) -> tuple[float, float]:
        if not self.moving:
            return 0, 0
        self.yaw = (self.yaw + 0.01) % (2 * math.pi)
        return -math.sin(self.yaw) * 0.2158, math.cos(self.yaw) * 0.2158

    def degrees(self) -> float:
        return (self.yaw * 180 / math.pi + 180) % 360 - 180


def subchunk_heights(dimension: int, limit: int) -> list[int]:
    lowest, count = {1: (0, 8), 2: (0, 16)}.get(dimension, (-4, 24))
    if limit >= 0:
        count = min(count, limit)
    return list(range(lowest, lowest + count))


def in_view(key: tuple[int, int, int], dimension: int, x: float, z: float, radius: int) -> bool:
    dim, cx, cz = key
    return dim == dimension and (cx - math.floor(x / 16)) ** 2 + (cz - math.floor(z / 16)) ** 2 <= (radius + 1) ** 2


class ChunkTracker:
    def __init__(self):
        self.origin: float | None = None
        self.seen: dict[tuple[int, int, int], float] = {}
        self.received = 0

    def start(self, now_ms: float):
        if self.origin is None:
            self.origin = now_ms

    def add(self, key: tuple[int, int, int], now_ms: float) -> bool:
        if self.origin is None:
            return False
        self.received += 1
        if key in self.seen:
            return False
        self.seen[key] = max(0, now_ms - self.origin)
        return True

    def view_count(self, dimension: int, x: float, z: float, radius: int) -> int:
        return sum(1 for _ in self._view_times(dimension, x, z, radius))

    def _view_times(self, dimension: int, x: float, z: float, radius: int):
        center_x, center_z = math.floor(x / 16), math.floor(z / 16)
        radius_squared = (radius + 1) ** 2
        for (dim, cx, cz), time in self.seen.items():
            if dim == dimension and (cx - center_x) ** 2 + (cz - center_z) ** 2 <= radius_squared:
                yield time

    def deltas(self, dimension: int, x: float, z: float, radius: int) -> list[int]:
        result, previous = [], 0
        for time in self._view_times(dimension, x, z, radius):
            current = max(previous, math.floor(time))
            result.append(current - previous)
            previous = current
        return result
