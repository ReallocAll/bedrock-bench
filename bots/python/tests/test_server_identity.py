# SPDX-License-Identifier: Apache-2.0
from __future__ import annotations

import asyncio
import json
import os
import socket
import sys
from types import SimpleNamespace

import psutil
import pytest
from bedrock_bench_bot import server_identity as probe
from cryptography.hazmat.primitives.asymmetric import ec
from nethernet.identity import (
    IdentitySigner,
    InvalidIdentity,
    cpk_digest,
    extract_identity,
)

PORT = 19672
LISTENER = probe.Listener("http://[::1]:19672", "::", 7)
SDP = "v=0\r\no=- 1 1 IN IP4 127.0.0.1\r\ns=-\r\nt=0 0\r\na=fingerprint:sha-256 AA:BB:CC\r\n"


def connection(address="::", pid=123, fd=7):
    return SimpleNamespace(
        family=socket.AF_INET6 if ":" in address else socket.AF_INET,
        laddr=SimpleNamespace(ip=address, port=PORT),
        status=psutil.CONN_LISTEN,
        pid=pid,
        fd=fd,
    )


def owner(monkeypatch, owned=None, inventory=None, created=456.25):
    owned = [connection()] if owned is None else owned
    inventory = owned if inventory is None else inventory
    process = SimpleNamespace(
        create_time=lambda: created,
        status=lambda: "running",
        net_connections=lambda **kwargs: owned,
        is_running=lambda: True,
    )
    monkeypatch.setattr(probe.psutil, "Process", lambda pid: process)
    monkeypatch.setattr(probe.psutil, "net_connections", lambda **kwargs: inventory)
    return process


@pytest.mark.parametrize(
    "address,endpoint",
    [
        ("::", "http://[::1]:19672"),
        ("::1", "http://[::1]:19672"),
        ("0.0.0.0", "http://127.0.0.1:19672"),
        ("127.0.0.2", "http://127.0.0.2:19672"),
    ],
)
def test_owned_listener_rechecks_harness_numeric_loopback(monkeypatch, address, endpoint):
    owner(monkeypatch, [connection(address)])
    probe.owned_listener(
        123,
        456.25,
        probe.Listener(endpoint, address, 7),
    )


@pytest.mark.parametrize(
    "inventory", [[connection(pid=None)], [connection(pid=124)], [connection(), connection(pid=124)]]
)
def test_listener_with_unknown_or_other_owner_rejects(monkeypatch, inventory):
    owner(monkeypatch, inventory=inventory)
    with pytest.raises(probe.IdentityProbeError, match="owner_unknown_or_ambiguous"):
        probe.owned_listener(123, 456.25, LISTENER)


def test_remote_only_listener_is_not_accepted(monkeypatch):
    owner(monkeypatch, [connection("192.0.2.1")])
    with pytest.raises(probe.IdentityProbeError, match="listener_changed"):
        probe.owned_listener(123, 456.25, LISTENER)


def test_process_or_listener_change_rejects(monkeypatch):
    process = owner(monkeypatch)
    probe.owned_listener(123, 456.25, LISTENER)
    process.create_time = lambda: 457.25
    with pytest.raises(probe.IdentityProbeError, match="process_identity_mismatch"):
        probe.owned_listener(123, 456.25, LISTENER)
    process.create_time = lambda: 456.25
    process.net_connections = lambda **kwargs: [connection(fd=8)]
    with pytest.raises(probe.IdentityProbeError, match="listener_changed"):
        probe.owned_listener(123, 456.25, LISTENER)


def test_exact_selection_accepts_other_same_process_binds_without_reselection(monkeypatch):
    owner(monkeypatch, [connection(), connection("0.0.0.0", fd=8), connection("127.0.0.2", fd=9)])
    selected = probe.Listener("http://127.0.0.2:19672", "127.0.0.2", 9)
    probe.owned_listener(123, 456.25, selected)


def test_dual_stack_other_owner_cannot_hide_behind_selected_ipv4(monkeypatch):
    selected = probe.Listener("http://127.0.0.1:19672", "0.0.0.0", 8)
    owner(monkeypatch, [connection("0.0.0.0", fd=8)], [connection("0.0.0.0", fd=8), connection("::", pid=124)])
    with pytest.raises(probe.IdentityProbeError, match="owner_unknown_or_ambiguous"):
        probe.owned_listener(123, 456.25, selected)


@pytest.mark.parametrize(
    "endpoint",
    [
        "http://localhost:19672",
        "http://192.0.2.1:19672",
        "http://127.0.0.1:19672",
        "http://[::1]:19673",
        "http://user:secret@[::1]:19672",
        "http://[::1]:19672/redirect",
    ],
)
def test_invalid_selection_cannot_choose_a_replacement(monkeypatch, endpoint):
    owner(monkeypatch)
    with pytest.raises(probe.IdentityProbeError):
        probe.owned_listener(123, 456.25, probe.Listener(endpoint, "::", 7))


class Response:
    def __init__(self, body, status=200):
        self.status = status
        self.body = body
        self.content = self

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return None

    async def iter_chunked(self, size):
        for index in range(0, len(self.body), size):
            yield self.body[index : index + size]


def exchange(monkeypatch, *, bad="", automatic_readiness=True):
    import aiohttp
    import nethernet.transport.peer_connection

    process = owner(monkeypatch)
    key = ec.generate_private_key(ec.SECP384R1())
    answer = IdentitySigner(key, domain="local.test").sign(SDP)
    if bad == "jwt":
        # Change the signed token, leaving its signature unchanged.
        _, envelope = extract_identity(answer)
        from nethernet.identity import build_envelope, insert_identity

        parts = envelope.token.split(".")
        parts[2] = ("A" if parts[2][0] != "A" else "B") + parts[2][1:]
        answer = insert_identity(SDP, build_envelope(envelope.domain, ".".join(parts), envelope.fingerprints))
    elif bad == "fingerprint":
        answer = answer.replace("AA:BB:CC", "DD:EE:FF")
    observed = {"closed": False, "requests": [], "http_closed": False, "applied": False}

    class Peer:
        def __init__(self, **kwargs):
            observed["state_changed"] = kwargs["on_connection_state_change"]
            observed["channels_ready"] = kwargs["on_channels_open"]

        async def create_offer(self):
            return SDP

        async def close(self):
            await asyncio.sleep(0)
            observed["closed"] = True
            if bad == "owner":
                process.create_time = lambda: 457.25
            if bad == "close":
                raise RuntimeError("close failed")

        async def set_remote_answer(self, answer):
            assert answer == SDP and "a=identity:" not in answer
            observed["applied"] = True
            if bad == "apply":
                raise ValueError("private SDP/token text must not escape")
            if automatic_readiness:
                observed["state_changed"]("connected")
                observed["channels_ready"]()

    class Http:
        def __init__(self, **kwargs):
            assert kwargs["trust_env"] is False
            assert kwargs["auto_decompress"] is False

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            observed["http_closed"] = True

        def request(self, method, url, **kwargs):
            assert kwargs["allow_redirects"] is False
            observed["requests"].append((method, url))
            if method == "GET":
                return Response(b"", 302 if bad == "redirect" else 200)
            return Response(b"x" * (probe.MAX_BODY + 1) if bad == "body" else answer.encode())

    monkeypatch.setattr(nethernet.transport.peer_connection, "PeerConnection", Peer)
    monkeypatch.setattr(aiohttp, "ClientSession", Http)
    return observed, key


def test_empty_capability_and_real_sdk_signed_identity(monkeypatch):
    observed, key = exchange(monkeypatch)
    result = asyncio.run(probe.probe(123, 456.25, LISTENER))
    assert result["verified"] is True and result["key_digest"] == cpk_digest(key.public_key())
    assert observed["closed"] and observed["http_closed"]
    assert [method for method, _ in observed["requests"]] == ["GET", "POST"]
    assert not any("player" in name for name in result)
    assert set(result) == {"verified", "key_digest", "endpoint", "pid", "create_time", "verified_at"}


@pytest.mark.parametrize(
    "bad,exception",
    [
        ("jwt", InvalidIdentity),
        ("fingerprint", InvalidIdentity),
        ("body", probe.IdentityProbeError),
        ("close", RuntimeError),
    ],
)
def test_invalid_identity_body_or_close_never_verifies(monkeypatch, bad, exception):
    observed, _ = exchange(monkeypatch, bad=bad)
    with pytest.raises(exception):
        asyncio.run(probe.probe(123, 456.25, LISTENER))
    assert observed["closed"] and observed["http_closed"]
    if bad in ("jwt", "fingerprint", "body"):
        assert not observed["applied"]


@pytest.mark.parametrize("first", ["connected", "channels"])
def test_verified_answer_waits_for_both_readiness_signals_before_close(monkeypatch, first):
    observed, _ = exchange(monkeypatch, automatic_readiness=False)

    async def run():
        task = asyncio.create_task(probe.probe(123, 456.25, LISTENER))
        await asyncio.sleep(0)
        assert observed["applied"] and not task.done() and not observed["closed"]
        if first == "connected":
            observed["state_changed"]("connected")
        else:
            observed["channels_ready"]()
        await asyncio.sleep(0)
        assert not task.done() and not observed["closed"]
        if first == "connected":
            observed["channels_ready"]()
        else:
            observed["state_changed"]("connected")
        result = await task
        assert result["verified"] and observed["closed"]

    asyncio.run(run())


@pytest.mark.parametrize("state", ["failed", "closed"])
def test_failed_or_closed_peer_never_verifies_and_still_closes(monkeypatch, state):
    observed, _ = exchange(monkeypatch, automatic_readiness=False)

    async def run():
        task = asyncio.create_task(probe.probe(123, 456.25, LISTENER))
        await asyncio.sleep(0)
        observed["state_changed"](state)
        with pytest.raises(probe.IdentityProbeError, match="peer_connection_failed"):
            await task
        assert observed["closed"] and observed["http_closed"]

    asyncio.run(run())


def test_readiness_timeout_still_closes_peer_and_http(monkeypatch):
    observed, _ = exchange(monkeypatch, automatic_readiness=False)
    with pytest.raises(TimeoutError):
        asyncio.run(probe.probe(123, 456.25, LISTENER, timeout=0.1))
    assert observed["applied"] and observed["closed"] and observed["http_closed"]


def test_apply_failure_cli_closes_and_emits_only_sanitized_json(monkeypatch, capsys):
    observed, _ = exchange(monkeypatch, bad="apply")
    assert (
        probe.main(
            [
                "--server-pid",
                "123",
                "--server-created",
                "456.25",
                "--endpoint",
                LISTENER.endpoint,
                "--listener-address",
                LISTENER.address,
                "--listener-fd",
                str(LISTENER.fd),
            ]
        )
        == 1
    )
    assert json.loads(capsys.readouterr().out) == {"verified": False, "error": "ValueError"}
    assert observed["applied"] and observed["closed"] and observed["http_closed"]


def test_redirect_has_no_post_and_is_not_followed(monkeypatch):
    observed, _ = exchange(monkeypatch, bad="redirect")
    with pytest.raises(probe.IdentityProbeError, match="redirect_rejected"):
        asyncio.run(probe.probe(123, 456.25, LISTENER))
    assert [method for method, _ in observed["requests"]] == ["GET"]
    assert observed["http_closed"]


def test_owner_change_after_exchange_still_closes_and_rejects(monkeypatch):
    observed, _ = exchange(monkeypatch, bad="owner")
    with pytest.raises(probe.IdentityProbeError, match="process_identity_mismatch"):
        asyncio.run(probe.probe(123, 456.25, LISTENER))
    assert observed["closed"]


def test_installed_isolated_cli_outputs_verified_json():
    """One real local test listener and installed SDK peer; this is not a BDS/game session."""
    from aiohttp import web
    from aiortc import RTCIceServer
    from nethernet.identity import verify_server_identity
    from nethernet.transport.peer_connection import PeerConnection

    async def run():
        key = ec.generate_private_key(ec.SECP384R1())
        methods = []
        packets = []
        channels_ready = asyncio.Event()
        peer = PeerConnection(
            is_dialer=False,
            ice_servers=[RTCIceServer(urls=[])],
            on_channels_open=channels_ready.set,
            on_packet=packets.append,
        )

        async def capability(request):
            methods.append(request.method)
            return web.Response(body=b"")

        async def signed_answer(request):
            methods.append(request.method)
            offer, _ = verify_server_identity(await request.text())
            answer = await peer.create_answer(offer)
            return web.Response(text=IdentitySigner(key, domain="local.test").sign(answer))

        app = web.Application()
        app.router.add_get("/v1/join", capability)
        app.router.add_post("/v1/join/{id}", signed_answer)
        runner = web.AppRunner(app)
        await runner.setup()
        listener = socket.socket()
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
        site = web.SockSite(runner, listener)
        await site.start()
        try:
            from bedrock_bench.phases import _owned_signaling_endpoint

            selected = _owned_signaling_endpoint(psutil.Process(), psutil.Process().create_time(), port)
            child = await asyncio.create_subprocess_exec(
                sys.executable,
                "-I",
                "-m",
                "bedrock_bench_bot.server_identity",
                "--server-pid",
                str(os.getpid()),
                "--server-created",
                str(psutil.Process().create_time()),
                "--endpoint",
                selected["endpoint"],
                "--listener-address",
                selected["address"],
                "--listener-fd",
                str(selected["fd"]),
                "--timeout",
                "8",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await asyncio.wait_for(child.communicate(), 10)
            assert child.returncode == 0, (stdout, stderr)
            result = json.loads(stdout)
            assert result["verified"] is True and result["key_digest"] == cpk_digest(key.public_key())
            assert result["endpoint"] == f"http://127.0.0.1:{port}"
            assert result["pid"] == os.getpid() and len(stdout) < 1024
            assert methods == ["GET", "POST"]
            assert channels_ready.is_set() and not packets
        finally:
            await peer.close()
            await runner.cleanup()

    asyncio.run(run())
