# Load bots

One client process runs every simulated player in a benchmark run. It reads
commands from stdin and writes JSON events to stdout; diagnostics go to stderr.
The harness starts the client, stores its events in `bots.jsonl.gz` and stops it.

| Module | Purpose |
| --- | --- |
| `core/` | Shared Go flags, events, movement, chunk accounting and fleet behavior. |
| `loadbot/` | Current protocol 2193 Go frontend, using gophertunnel. Supports RakNet and NetherNet. |
| `legacy1001/` | Protocol 1001 Go frontend for PocketMine-MP and Allay. |
| [python/](python/README.md) | Protocol 2193 RakNet and NetherNet frontend using the installed Python SDK. |

A session uses one implementation and the same workload flags for every server.
Only `-fake-xuid` may be added by a server adapter. Python supports protocol 2193
only and does not support Go's manual JSON control commands.

## Build or fetch Go clients

```shell
uv run bbench bots build
uv run bbench bots build --module legacy1001
uv run bbench bots build --target linux/amd64
uv run bbench bots fetch
```

Build requires Go 1.25+; each module's `go.mod` selects its toolchain. Fetch downloads
release binaries and verifies their published checksums.

Clients and manifests are stored under `bots/bin/<os>-<arch>/`. Before a run, the
harness records the executable identity and `-version` output. Go builds use each
module's own `go.mod` and `go.sum`, with the development workspace disabled.
For Python installation and workload selection, see the
[Python client guide](python/README.md).

### Python installation

Default Python fetch downloads the exact universal adapter wheel and verifies its
release `SHA256SUMS` before replacing the managed environment:

```shell
uv run bbench bots fetch --module python
```

This path is **not yet usable** until a matching release and compatible SDK/native
dependencies are available. No bot releases are currently published. Development
harness versions also require an explicit released tag through `BBENCH_BOTS_TAG`.
Missing assets and checksum mismatches are errors, with no package-index fallback.
The adapter wheel does not contain or publish the SDK and native dependencies.

Python defaults to releases under `ReallocAll/bedrock-bench`; Go retains
`EndstoneMC/bedrock-bench`. `BBENCH_BOTS_URL` overrides the release download base
and `BBENCH_BOTS_TAG` selects a tag for either implementation.

Both Python build and fetch use a managed Python 3.12 environment by default.
`--python` selects another compatible interpreter and alone retains verified
release fetching. Explicit `--find-links`, `--no-index` or `--requirement` inputs
select the existing development installation path instead, for example:

```shell
uv run bbench bots fetch --module python --python 3.14 --find-links /path/to/wheels --requirement /path/to/sdk-requirements.txt
```

Use the native wheel's matching Python version. Requirements paths resolve from
the caller's directory and their hashes are recorded. `--no-index` disables index
lookups, not explicitly requested Git/network sources. Build uses the same
installer, dependency checks and installed-file identity capture. Direct adapter
dependencies are pinned in its metadata; this is not a full transitive lock.

RakNet is an optional SDK dependency. Include `bedrock-bot[raknet]==0.1.0` in the
requirements supplied to `bots build` or `bots fetch` when installing a RakNet
workload. The adapter wheel also exposes a `raknet` extra for direct package
installation.

Use `configs/smoke-python.toml` or `configs/spread-100-python.toml` for Python over
NetherNet with managed local identity. Use `configs/smoke-python-raknet.toml` or
`configs/spread-100-python-raknet.toml` for Python over RakNet with configured
identity. See the [Python guide](python/README.md#select-a-workload) for details.

### Protocol 1001

The legacy module requires the fork tag declared by its module metadata.

## Flags

The harness normally supplies these from the workload config. For a manual Go
session:

```shell
loadbot -addr 127.0.0.1:19672 -n 4 -prefix a
```

Type `quit` or end stdin to stop. `-version` prints build information;
`-selftest-output` emits synthetic events and exits without connecting.

| Flag | Default | Meaning |
| --- | --- | --- |
| `-addr HOST:PORT` | required | Server address; no default port. |
| `-transport` | `raknet` (Go), `nethernet` (Python) | Python supports both transports for protocol 2193. |
| `-server-key-sha256` | empty | Required for configured Python NetherNet; optional for Python RakNet. |
| `-n` | 1 | Number of bots. |
| `-prefix` | `b` | Names use prefix plus an index of at least two digits. |
| `-stagger` | 300ms | Time between first join starts. |
| `-csg` | false | Advertise client-side chunk generation; benchmark configs leave it off. |
| `-radius` | 16 | Requested chunk radius; the server may clamp it. Spawn initially requests 16. |
| `-loadscreen` | true | Loading-screen messages for spawn and dimension changes. BDS uses completion to enable movement. |
| `-walk` | `circle` | `circle` moves 0.2158 blocks and turns 0.01 radians per tick; `none` stands still. Both schedule input at 20 Hz. |
| `-spawn-timeout` | 60s | Dial, login and spawn deadline per attempt. |
| `-join-retries` | 0 | Additional failed-first-join attempts, three seconds apart. |
| `-fake-xuid` | false | Declare offline XUID 2535400000000000 + index. |
| `-identity-seed` | `bedrock-bench` | Namespace seed for deterministic player UUIDs. |
| `-control` | false | Go-only manual JSON control commands. |
| `-progress-interval` | 5s | Progress reporting interval. |
| `-stat-interval` | 10s | Statistics and enabled RTT reporting interval. |
| `-pkt-interval` | 60s | Packet delta reporting interval. |
| `-probe-bots` | 0 | Lowest-index bots that send a latency probe each second. |
| `-decode-errors` | `count` | Report recoverable errors and continue, or `fail` to exit 3 at the first error. Fatal framing/transport errors still close a session. |

### Commands and exit codes

Both implementations accept `quit` and stop on EOF. Go additionally accepts these
JSON lines with `-control`; address a bot by name (`bot`) or index (`i`):

| Command | Effect |
| --- | --- |
| `{"op":"reconnect","bot":"a07"}` | Close and reconnect after `delay_ms` (default 3000), including an offline bot. |
| `{"op":"disconnect","i":7}` | Close and remain offline. |
| `{"op":"radius","radius":8}` | Request a radius for the addressed bot, or every online bot if unaddressed. |
| `{"op":"stop"}` | Stop the process. |

Unknown or malformed input is ignored with a diagnostic. Exit 0 means normal
stop; 1 means invalid flags or a client failure; 2 means every first join failed;
3 means `-decode-errors fail` stopped the client.

## Workload behavior

- **Identity:** offline, self-signed login. Player UUIDs are UUIDv5(namespace,
  name). A UUID seed is used directly; other seeds use UUIDv5 of the URL namespace.
  The same seed and names identify the same players across runs. No resource packs
  are downloaded.
- **Chunks:** the blob cache is enabled, but every requested blob is reported
  missing so the server sends full data. Sub-chunk requests start at -4 in the
  overworld and 0 in other dimensions, honoring an announced limit or requesting
  the whole column.
- **Movement:** initial yaw is index × 137.508 degrees. Input uses the latest
  server position and a fixed client height; teleports are acknowledged and
  corrections replace the position.
- **Upkeep:** answer server latency requests, acknowledge dimension changes and
  request respawn after death, with at most four attempts ten seconds apart.

## Events

Events follow [bot-event.schema.json](../bedrock_bench/schemas/bot-event.schema.json).
Each line has `v`, `ev`, UTC epoch milliseconds `t` and monotonic `mono_ms` since
client start. The harness adds receipt timestamp `rx_ns`.

A chunk is a column keyed by dimension and chunk coordinates. Before the first
teleport, received columns belong to the spawn area and do not enter the view
tracker. `uniq` counts distinct tracked columns. `in_view` counts tracked columns
in the current dimension within a Euclidean radius of the granted radius + 1,
centered on the player's current chunk. Negative coordinates use floor division.
Chunks streaming from an old view do not count toward the current view.

`first_chunk` measures the first in-view arrival after the first teleport.
Per-bot progress and summaries describe a session and reset on rejoin. Application
packet counters accumulate across spawned sessions; failed
join attempts and login traffic do not contribute. Promoted pre-spawn application
packets count only after that attempt spawns. Application counters exclude
incoming Disconnect packets.

Go RakNet byte counters measure UDP payloads, including framing and resends.
NetherNet byte counts are unknown: Python emits `null`; Go emits zero, which must
not be interpreted as measured zero. The Python SDK does not expose transport
bytes and emits `null` for either transport. Socket closure is a drop, not a
decode error. Decode errors invalidate a benchmark run.

| Event | Meaning and principal fields |
| --- | --- |
| `hello` | Build, effective flags and reporting intervals. |
| `dial` | Join attempt started: `bot`, `i`. |
| `spawned` | Joined the world: `bot`, `i`, `session`, `join_ms`, `radius`, `dim`. |
| `join_failed` | Failed attempt: `stage`, `err`, `attempt`, `final`; only final first-session failures count as failed bots. |
| `tp`, `dim` | Teleport or dimension update. |
| `first_chunk` | First in-view chunk delay, `dt_ms`. |
| `progress` | Rows: `[bot, uniq, in_view, x, y, z, dim, corrections, teleports]`; coordinates are null before the first teleport. |
| `stat` | Current fleet state and cumulative counters. |
| `pkt` | `rx` and `tx` packet deltas since the previous report, by packet name. |
| `rtt` | Probe count, nearest-rank p50/p99/max milliseconds and lost probes; enabled with `-probe-bots`. |
| `died`, `respawned` | Death and respawn. |
| `left`, `rejoin` | Go manual control closed a session or started a later session. |
| `dropped`, `kicked` | Transport failure or server disconnect, with its reason. |
| `decode_error` | `pkt_id`, `err`, cumulative `total`; first ten errors are reported, then at most one per second. |
| `alljoined` | Every bot has spawned or exhausted its first-join attempts: `joined`, `failed`. |
| `bot_summary` | Last spawned session's `uniq`, `in_view`, `chunks`, `corrections`, `first_seen_ms`. |
| `done` | Final event. |

`stat` includes `online`, `joined`, `failed`, `dropped`, `kicked`, `left`,
`rejoins`, `rejoin_failed`, `deaths`, packet and byte totals, `corrections`,
`level_chunks`, `subchunks`, `uniq`, `in_view` and `decode_errors`. `online` and
`in_view` are current values; the others are cumulative. Implementation-specific
optional measurements are described in the [Python guide](python/README.md#measurements).

In summaries, `chunks` includes duplicate tracked chunk packets. `first_seen_ms`
is one entry per currently in-view column, in first-arrival order, encoded as
millisecond deltas since the first teleport. Shutdown emits final reports, closes
sessions, writes per-bot summaries and then `done`.

## Contributing

A Go frontend implements `core.Frontend`: establish a session, call
`Session.Spawned`, report packets through `Session` and return when the session
ends. Protocol versions with incompatible dependency versions remain separate
Go modules.

Run checks in each affected module with the workspace disabled:

```shell
cd bots/core
GOWORK=off go vet ./...
GOWORK=off go test ./...
```

Repeat for `loadbot` or `legacy1001` as needed. Shared event fixtures are validated
against the JSON schema; protocol frontend tests include controlled login and
packet handling. Real-server smoke tests are separate from these tests.

### Patches

The current-protocol frontend uses upstream gophertunnel without carried patches.
The legacy frontend carries an [offline-login backport](legacy1001/0001-login-backport.patch)
from upstream [#468](https://github.com/Sandertv/gophertunnel/pull/468) and
[#481](https://github.com/Sandertv/gophertunnel/pull/481). Dependency versions and
fork tags belong to each module's `go.mod` and `go.sum`.

#### Policy

A carried patch needs a failing regression, an upstream report and an explicit
removal condition. Keep the patch file, immutable fork tag, module checksums and
`carriedPatches` declaration consistent. Published Go module tags must not be
rewritten. Remove the login backport when the legacy server accepts the current
frontend; remove the legacy frontend only when no supported server needs it.

See [contributor guidance](../.github/CONTRIBUTING.md) for release checks and
[methodology](../docs/methodology.md) for shared workload rules.
