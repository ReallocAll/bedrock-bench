# SPDX-License-Identifier: Apache-2.0
"""Use the selected SDK's own controlled peer and binary fixtures."""

import importlib.util
from pathlib import Path

import pytest


def pytest_addoption(parser):
    parser.addoption(
        "--sdk-source", type=Path, help="Extracted endpilot sdist (or SDK checkout for focused local tests)"
    )


@pytest.fixture(scope="session")
def sdk_peer(pytestconfig):
    source = pytestconfig.getoption("sdk_source")
    if source is None:
        raise pytest.UsageError("SDK integration requires --sdk-source pointing to the extracted endpilot sdist")
    peer = source.resolve() / "tests/python/peer.py"
    fixture = peer.parent / "fixtures/start_game_2193.bin"
    for path in (peer, fixture):
        if not path.is_file():
            raise pytest.UsageError(f"--sdk-source is missing required SDK test input: {path}")
    spec = importlib.util.spec_from_file_location("controlled_sdk_peer", peer)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module
