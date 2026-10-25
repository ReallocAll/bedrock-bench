# Python load client

`bedrock-bench-bot` runs protocol 2193 benchmark players over NetherNet or RakNet
using the `bedrock-bot` SDK. It follows the shared [load bot workload](../README.md),
including movement, chunk requests, startup accounting and JSON events.

It supports Python 3.12+ on Windows and Linux with compatible native protocol
wheels. Protocol 1001 and Go's `-control` commands are not supported.

## Install

The harness installs this package and its SDK dependencies into
`bots/bin/<os>-<arch>/python-env`. Supply the required wheels through the configured
package index or a local wheel directory. A native wheel's Python tag and platform
must match the selected interpreter. The SDK's base dependency includes NetherNet;
RakNet's implementation is selected by the optional `raknet` extra. The unrelated
registry package with the same name is not a substitute.

The installer uses uv and passes supplied requirements files through unchanged.
`--no-index` disables package-index lookups; it does not disable Git downloads or
other explicitly supplied network sources.

For example, with compatible wheels and a trusted requirements file available:

```shell
uv run bbench bots build --module python --python 3.14 --find-links /path/to/wheels --requirement /path/to/sdk-requirements.txt
```

Select the Python version that matches your native wheel; 3.14 is only the example
above. The requirements file must explicitly select dependencies that need direct
Git URLs or development versions, using the SDK's dependency metadata. Requirements
paths and `--find-links` may be supplied more than once. Use `bots fetch` with the
same options when the adapter wheel is already available and no source build is
needed. Add `--no-index` only when package-index lookups are unwanted.

Installation supports the current host only. The harness launches the installed
interpreter with `-I -m bedrock_bench_bot`, without importing this source checkout.
The console command is `bedrock-bench-bot`; `-version` prints the installed client
and dependency identities, and `-selftest-output` checks event output without
connecting to a server.

## Select a workload

For a local BDS or Endstone NetherNet smoke run, save `python-smoke.toml` at the
repository root:

```toml
base = "smoke"

[bots]
implementation = "python"
transport = "nethernet"
server_key_source = "managed-local"
server_key_sha256 = ""
```

```shell
uv run bbench smoke python-smoke.toml bds@1.26.52.3
```

Managed local identity verifies the actual local server process and its signaling
listener, then resolves its signed public identity key for the bot connections.
It supports only local BDS and Endstone and requires an empty configured digest.
Unknown listener ownership, redirects or identity failure abort the run. Command
overrides through `BBENCH_BOT_CMD` are not supported in this mode.

To use a digest obtained through a trusted channel, set
`server_key_source = "configured"` (the default) and supply
`server_key_sha256` as 64 hexadecimal characters. Configured NetherNet workloads
require that pin. RakNet accepts an empty pin or a 64-character digest. Every SDK
connection verifies a supplied digest. A mismatched configured key never falls
back to managed identity. Managed local identity remains limited to local BDS and
Endstone NetherNet. The SDK can connect to remote endpoints; managed local
discovery is a benchmark-specific convenience, not an SDK address restriction.

The harness configures the server and client for the same transport. Python
workloads require protocol 2193; NetherNet additionally requires a supported BDS
or Endstone adapter.

The inherited `configs/smoke-python-raknet.toml` and
`configs/spread-100-python-raknet.toml` presets select RakNet and configured
identity with an empty digest. Install `bedrock-bot[raknet]==0.1.0` alongside the
adapter when using them. The existing Python presets continue to use NetherNet
and managed local identity.

## Measurements

All events follow the shared bot schema. Two optional `stat` fields provide
additional Python measurements:

- `input_packets` maps each spawned bot name to successfully sent
  `PlayerAuthInput` packets from its latest spawned session's SDK play histogram.
  It remains present for bots that disconnect. It counts completed submissions,
  not scheduled ticks, and is cumulative within that session.
- `tick_lateness_us_max` is the maximum observed `TickEvent.lateness_us` since the
  previous stat report, then resets. It measures ticks that actually ran; it is
  neither a percentile nor a count of every missed 50 ms period.

Transport bytes are unavailable from the Python SDK for both transports and are
emitted as `null`. Use application packet counts to assess input traffic; online
player count alone does not prove sustained input. The build metadata records the
actual running loop and uvloop version where used. Linux uses uvloop; Windows uses
a Selector event loop.

## Startup and shutdown

Startup accounting follows the Go frontend's no-download sequence. Successful
startup application traffic is promoted once; failed attempts do not contribute.
Deferred work is bounded to 4096 compact records and 65536 blob IDs per attempt;
overflow fails that attempt. The SDK owns protocol acknowledgements, latency
responses and respawn sequencing.

`quit`, EOF, SIGINT and SIGTERM stop the client and write final statistics and
summaries. Windows also supports Ctrl+Break. Sessions that spawned are not
reconnected automatically. `-join-retries` applies only to failed first joins.

## Contributing

The adapter owns benchmark policy; general connection and gameplay behavior
belongs in the SDK. Preserve the shared event schema, accounting windows and
workload when changing either implementation. See the
[SDK API guide](https://github.com/ReallocAll/bedrock-bot/blob/main/docs/api.md)
for SDK lifetime and error contracts.

In an isolated environment with compatible SDK/protocol wheels available, install
the harness for its packaged schema, build and install the adapter, and run its
tests from the benchmark repository root:

```shell
python -m pip install build .
python -m build bots/python --outdir bots/python/dist
python -m pip install --find-links ../bedrock-bot/dist --find-links ../bedrock-protocol/dist "bots/python/dist/bedrock_bench_bot-0.1.0.dev0-py3-none-any.whl[test]"
python -I -m pytest bots/python/tests --sdk-source ../bedrock-bot-test-source/bedrock_bot-0.1.0
```

Adjust wheel filenames and paths to your build. `--sdk-source` is required for the
SDK integration tests and points to the matching SDK's extracted source
distribution, which supplies its controlled peer and binary fixtures. The adapter
and SDK themselves are imported from their installed wheels; the harness schema
comes from the installed `bedrock-bench` package. Missing SDK test inputs fail
explicitly rather than skipping integration coverage.
