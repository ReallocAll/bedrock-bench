# SPDX-License-Identifier: Apache-2.0
from __future__ import annotations

import argparse
import math
import re
from dataclasses import asdict, dataclass
from urllib.parse import urlsplit


class UsageError(ValueError):
    pass


_UNITS = {"ns": 1e-9, "us": 1e-6, "µs": 1e-6, "μs": 1e-6, "ms": 1e-3, "s": 1, "m": 60, "h": 3600}
_DURATION = re.compile(r"(\d+(?:\.\d*)?|\.\d+)(ns|us|µs|μs|ms|s|m|h)")


def duration(text: str) -> float:
    """Parse Go time.Duration spellings into seconds, without accepting unitless values."""
    original = text
    sign = -1 if text.startswith("-") else 1
    if text.startswith(("-", "+")):
        text = text[1:]
    if text == "0":
        return 0
    total, offset = 0.0, 0
    for match in _DURATION.finditer(text):
        if match.start() != offset:
            raise argparse.ArgumentTypeError(f"invalid duration {original!r}")
        total += float(match[1]) * _UNITS[match[2]]
        offset = match.end()
    if not text or offset != len(text) or not math.isfinite(total) or total > (2**63 - 1) / 1e9:
        raise argparse.ArgumentTypeError(f"invalid duration {original!r}")
    return sign * total


def boolean(value: str | bool) -> bool:
    if isinstance(value, bool):
        return value
    if value in ("1", "t", "T", "TRUE", "true", "True"):
        return True
    if value in ("0", "f", "F", "FALSE", "false", "False"):
        return False
    raise argparse.ArgumentTypeError(f"invalid boolean {value!r}")


def duration_text(seconds: float) -> str:
    if seconds == 0:
        return "0s"
    if abs(seconds) < 1:
        for scale, unit in ((1e-3, "ms"), (1e-6, "µs"), (1e-9, "ns")):
            if abs(seconds) >= scale:
                return f"{seconds / scale:g}{unit}"
    sign = "-" if seconds < 0 else ""
    seconds = abs(seconds)
    hours, seconds = divmod(seconds, 3600)
    minutes, seconds = divmod(seconds, 60)
    if hours:
        return f"{sign}{int(hours)}h{int(minutes)}m{seconds:g}s"
    if minutes:
        return f"{sign}{int(minutes)}m{seconds:g}s"
    return f"{sign}{seconds:g}s"


@dataclass(frozen=True, slots=True)
class Config:
    addr: str = ""
    transport: str = "nethernet"
    n: int = 1
    prefix: str = "b"
    stagger: float = 0.3
    csg: bool = False
    radius: int = 16
    loadscreen: bool = True
    walk: str = "circle"
    spawn_timeout: float = 60
    join_retries: int = 0
    fake_xuid: bool = False
    identity_seed: str = "bedrock-bench"
    control: bool = False
    progress_interval: float = 5
    stat_interval: float = 10
    pkt_interval: float = 60
    probe_bots: int = 0
    decode_errors: str = "count"
    server_key_sha256: str = ""
    selftest_output: bool = False
    version: bool = False

    def validate(self):
        if self.transport != "nethernet":
            raise UsageError("-transport must be nethernet; this frontend supports protocol 2193 only")
        if self.control:
            raise UsageError("-control is not supported; stdin accepts plain quit and EOF")
        try:
            parsed = urlsplit("http://" + self.addr)
            valid_addr = (
                parsed.hostname
                and parsed.port
                and parsed.netloc == self.addr
                and not parsed.path
                and not parsed.query
                and not parsed.fragment
                and not parsed.username
                and not parsed.password
            )
        except ValueError:
            valid_addr = False
        if not valid_addr:
            raise UsageError("-addr must be HOST:PORT (IPv6 addresses must be bracketed)")
        if not re.fullmatch(r"[0-9a-fA-F]{64}", self.server_key_sha256):
            raise UsageError("-server-key-sha256 requires the server's 64 hexadecimal SHA-256 fingerprint")
        if self.n < 1 or not 0 <= self.probe_bots <= self.n:
            raise UsageError("-n must be at least 1 and -probe-bots must be in 0..n")
        if not self.prefix or any(c in self.prefix for c in ' \t\r\n"'):
            raise UsageError("-prefix must be nonempty without spaces or quotes")
        if not self.identity_seed:
            raise UsageError("-identity-seed must not be empty")
        if self.stagger < 0 or self.spawn_timeout <= 0 or self.join_retries < 0:
            raise UsageError("stagger and join-retries must be nonnegative; spawn-timeout must be positive")
        if not 1 <= self.radius <= 96:
            raise UsageError("-radius must be in 1..96")
        if self.walk not in ("circle", "none") or self.decode_errors not in ("count", "fail"):
            raise UsageError("-walk must be circle or none; -decode-errors must be count or fail")
        if min(self.progress_interval, self.stat_interval, self.pkt_interval) < 0.001:
            raise UsageError("reporting intervals must be at least 1ms")
        if not 1 <= math.ceil(self.spawn_timeout * 1000) <= 0xFFFFFFFF:
            raise UsageError("-spawn-timeout must fit the SDK's positive unsigned 32-bit milliseconds")

    def name(self, index: int) -> str:
        return f"{self.prefix}{index:02d}"

    def flags(self) -> dict:
        result = {}
        for name, value in asdict(self).items():
            if name in ("version", "selftest_output"):
                continue
            if name in ("stagger", "spawn_timeout", "progress_interval", "stat_interval", "pkt_interval"):
                value = duration_text(value)
            result[name.replace("_", "-")] = value
        return result

    def intervals(self) -> dict:
        return {name: int(getattr(self, name + "_interval") * 1000) for name in ("progress", "stat", "pkt")}


class Parser(argparse.ArgumentParser):
    def error(self, message):
        raise UsageError(message)


def parse_args(args: list[str]) -> Config:
    parser = Parser(prog="bedrock-bench-bot", allow_abbrev=False)
    defaults = Config()
    for name, value in asdict(defaults).items():
        flag = "-" + name.replace("_", "-")
        if type(value) is bool:
            parser.add_argument(flag, type=boolean, nargs="?", const=True, default=value)
        else:
            kind = (
                duration
                if name in ("stagger", "spawn_timeout", "progress_interval", "stat_interval", "pkt_interval")
                else type(value)
            )
            parser.add_argument(flag, type=kind, default=value)
    return Config(**vars(parser.parse_args(args)))
