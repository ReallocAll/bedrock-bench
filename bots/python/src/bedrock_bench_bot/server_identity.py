# SPDX-License-Identifier: Apache-2.0
"""Verify a public NetherNet identity anchored to the harness's local server process.

This verifies one signaling exchange and completes DTLS/data-channel setup without game login.
Only public verification results leave this process; SDP, tokens and generated keys stay in memory.
"""

from __future__ import annotations

import argparse
import asyncio
import ipaddress
import json
import math
import socket
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from urllib.parse import urlsplit
from uuid import uuid4

import psutil

MAX_BODY = 1024 * 1024


class IdentityProbeError(RuntimeError):
    """A fixed diagnostic code, never library/HTTP/SDP exception text."""


@dataclass(frozen=True)
class Listener:
    endpoint: str
    family: int
    address: str
    port: int
    fd: int


def _matches(connection, host: str, port: int) -> bool:
    """Whether a listening bind can accept this numeric endpoint, including dual-stack wildcards."""
    if connection.status != psutil.CONN_LISTEN or not connection.laddr or connection.laddr.port != port:
        return False
    address = ipaddress.ip_address(connection.laddr.ip)
    selected = ipaddress.ip_address(host)
    if address.version == selected.version:
        return address.is_unspecified or address == selected
    # A wildcard IPv6 listener may accept IPv4. psutil does not expose IPV6_V6ONLY, so fail closed on another owner.
    return selected.version == 4 and address.version == 6 and address.is_unspecified


def owned_listener(pid: int, created: float, expected: Listener) -> None:
    """Recheck the harness's exact selection; never choose or substitute a listener."""
    try:
        endpoint = urlsplit(expected.endpoint)
        address = ipaddress.ip_address(expected.address)
        target = ipaddress.ip_address(endpoint.hostname or "")
        if (
            endpoint.scheme != "http"
            or not target.is_loopback
            or endpoint.port != expected.port
            or endpoint.username is not None
            or endpoint.password is not None
            or endpoint.path
            or endpoint.query
            or endpoint.fragment
            or "%" in endpoint.netloc
            or expected.family != (socket.AF_INET6 if address.version == 6 else socket.AF_INET)
            or target.version != address.version
            or not (address.is_unspecified or address == target)
        ):
            raise IdentityProbeError("invalid_listener_selection")
        process = psutil.Process(pid)
        if process.create_time() != created or process.status() == psutil.STATUS_ZOMBIE:
            raise IdentityProbeError("server_process_identity_mismatch")
        if not any(
            connection.status == psutil.CONN_LISTEN
            and connection.laddr
            and (connection.family, connection.laddr.ip, connection.laddr.port, connection.fd)
            == (expected.family, expected.address, expected.port, expected.fd)
            for connection in process.net_connections(kind="tcp")
        ):
            raise IdentityProbeError("server_listener_changed")
        inventory = [
            connection
            for connection in psutil.net_connections(kind="tcp")
            if _matches(connection, str(target), expected.port)
        ]
        if not inventory or any(connection.pid != pid for connection in inventory):
            raise IdentityProbeError("endpoint_owner_unknown_or_ambiguous")
        if not any(
            connection.family == expected.family and connection.laddr.ip == expected.address for connection in inventory
        ):
            raise IdentityProbeError("endpoint_owner_unknown_or_ambiguous")
        if process.create_time() != created or not process.is_running():
            raise IdentityProbeError("server_process_changed_during_listener_check")
    except psutil.Error:
        raise IdentityProbeError("server_ownership_unavailable") from None
    except ValueError:
        raise IdentityProbeError("server_listener_address_invalid") from None


async def _http_body(http, method: str, url: str, *, data: bytes | None = None) -> bytes:
    async with http.request(
        method,
        url,
        data=data,
        allow_redirects=False,
        headers={"Content-Type": "application/sdp"} if data is not None else None,
    ) as response:
        if 300 <= response.status < 400:
            raise IdentityProbeError("http_redirect_rejected")
        if response.status != 200:
            raise IdentityProbeError("http_status_rejected")
        body = bytearray()
        async for chunk in response.content.iter_chunked(16384):
            if len(body) + len(chunk) > MAX_BODY:
                raise IdentityProbeError("http_response_too_large")
            body.extend(chunk)
        return bytes(body)


async def probe(pid: int, created: float, listener: Listener, timeout: float = 10.0) -> dict:
    """Return a verified digest only after the signaling peer closes and ownership is rechecked."""
    if pid <= 0 or not math.isfinite(created) or created <= 0 or not 1 <= listener.port <= 65535:
        raise IdentityProbeError("invalid_process_or_port")
    if not math.isfinite(timeout) or not 0 < timeout <= 10:
        raise IdentityProbeError("invalid_timeout")
    deadline = time.monotonic() + timeout
    owned_listener(pid, created, listener)
    import aiohttp
    from aiortc import RTCIceServer
    from bedrock_bot.auth import create_identity
    from nethernet.identity import (
        IdentitySigner,
        build_envelope,
        insert_identity,
        sign_fingerprints,
        verify_server_identity,
    )
    from nethernet.network_id import NetworkID, new_connection_id
    from nethernet.transport.peer_connection import PeerConnection

    identity = create_identity("BenchPinProbe", str(uuid4()))

    class OfflineSigner(IdentitySigner):
        def sign(self, sdp: str) -> str:
            return insert_identity(
                sdp, build_envelope("offline.endstone.invalid", identity.token, sign_fingerprints(sdp, identity.key))
            )

    signer = OfflineSigner(identity.key, domain="offline.endstone.invalid")
    peer = None
    try:
        async with asyncio.timeout_at(deadline - min(2.0, timeout * 0.3)):
            async with aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=timeout),
                trust_env=False,
                auto_decompress=False,
            ) as http:
                # An empty successful capability response is valid. It makes no player-count claim.
                owned_listener(pid, created, listener)
                await _http_body(http, "GET", listener.endpoint + "/v1/join")
                readiness_changed = asyncio.Event()
                connection_state = "new"
                channels_open = False

                def state_changed(state: str) -> None:
                    nonlocal connection_state
                    connection_state = state
                    readiness_changed.set()

                def channels_ready() -> None:
                    nonlocal channels_open
                    channels_open = True
                    readiness_changed.set()

                peer = PeerConnection(
                    is_dialer=True,
                    ice_servers=[RTCIceServer(urls=[])],
                    on_connection_state_change=state_changed,
                    on_channels_open=channels_ready,
                )
                offer = signer.sign(await peer.create_offer())
                owned_listener(pid, created, listener)
                answer = await _http_body(
                    http,
                    "POST",
                    listener.endpoint + "/v1/join/" + str(NetworkID.p2p(new_connection_id())),
                    data=offer.encode("utf-8"),
                )
                if not answer.startswith(b"v="):
                    raise IdentityProbeError("answer_is_not_sdp")
                verified_answer, verified = verify_server_identity(answer.decode("utf-8"))
                await peer.set_remote_answer(verified_answer)
                while connection_state != "connected" or not channels_open:
                    if connection_state in ("failed", "closed"):
                        raise IdentityProbeError("peer_connection_failed")
                    readiness_changed.clear()
                    await readiness_changed.wait()
    finally:
        if peer is not None:
            async with asyncio.timeout_at(deadline):
                await peer.close()
    owned_listener(pid, created, listener)
    if time.monotonic() >= deadline:
        raise IdentityProbeError("identity_probe_timeout")
    return {
        "verified": True,
        "key_digest": verified.key_digest,
        "endpoint": listener.endpoint,
        "pid": pid,
        "create_time": created,
        "verified_at": datetime.now(UTC).isoformat(),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--server-pid", required=True, type=int)
    parser.add_argument("--server-created", required=True, type=float)
    parser.add_argument("--endpoint", required=True)
    parser.add_argument("--listener-family", required=True, type=int)
    parser.add_argument("--listener-address", required=True)
    parser.add_argument("--port", required=True, type=int)
    parser.add_argument("--listener-fd", required=True, type=int)
    parser.add_argument("--timeout", type=float, default=10.0)
    args = parser.parse_args(argv)
    try:
        listener = Listener(args.endpoint, args.listener_family, args.listener_address, args.port, args.listener_fd)
        result = asyncio.run(probe(args.server_pid, args.server_created, listener, args.timeout))
    except Exception as exc:
        code = str(exc) if isinstance(exc, IdentityProbeError) else type(exc).__name__
        print(json.dumps({"verified": False, "error": code}))
        return 1
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
