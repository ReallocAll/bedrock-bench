# bedrock-bench

A load benchmark for Minecraft Bedrock servers. It creates a fresh world, spreads
headless players across it and measures tick rate, CPU, memory and chunk loading.
Each server receives the same workload and measurement rules. Results remain
separate metrics; there is no composite score.

## Quick start

Requires Windows x64 or Linux x86_64, Python 3.11+, [uv](https://docs.astral.sh/uv/)
and Go 1.25+ to build the default load bot. Reserve at least four logical CPUs for
bots in addition to the server's CPUs. Server-specific toolchains are documented
in the corresponding adapter directory.

```shell
git clone https://github.com/EndstoneMC/bedrock-bench.git
cd bedrock-bench
uv sync
uv run bbench doctor
uv run bbench bots build
uv run bbench fetch bds@1.26.52.3 --accept-eula
uv run bbench smoke configs/smoke.toml bds@1.26.52.3
```

`doctor` checks machine setup and toolchains. The smoke workload uses four bots
and short phases to check the installation before a full benchmark.
`--accept-eula` accepts the [Minecraft EULA](https://www.minecraft.net/eula) for
BDS and the LeviMC EULA when fetching LeviLamina. Server binaries and worlds are
not included in this repository.

See [Getting started](docs/get_started.md) for machine profiles and output files.
For comparable bare-metal results, use an idle physical machine. VM and WSL2 runs
are recorded as `vcpu` and must not be mixed with bare-metal measurements.

## Run a workload

```shell
uv run bbench run configs/spread-100.toml bds@1.26.52.3 endstone@0.11.14
```

The config comes first, followed by optional server selectors. Omitting selectors
runs the supported defaults. Download them first with
`uv run bbench fetch --accept-eula`.

The [spread workload](configs/spread-100.toml) uses 100 bots joining 300 ms apart,
a grid with 640 blocks between players, view distance 10, simulation distance 4
and server-side chunk generation. Each bot walks at a fixed height. Runs progress
through boot, idle, join, settle, a 90-second steady window and teardown. The config
also sets repeats and their order.

Use [spread-50](configs/spread-50.toml) or [spread-25](configs/spread-25.toml) when
the client machine cannot sustain 100 bots. Compare results only within the same
workload, machine and placement settings.

The default client is Go over RakNet. Go also supports NetherNet; the optional
[Python client](bots/python/README.md) uses the installed `bedrock-bot` SDK over
NetherNet or RakNet. [Load bot documentation](bots/README.md) describes flags,
accounting and implementation differences.

The inherited `configs/smoke-python.toml` and `configs/spread-100-python.toml`
presets select Python over NetherNet with managed local server identity; all
other workload settings remain inherited. The
`configs/smoke-python-raknet.toml` and `configs/spread-100-python-raknet.toml`
presets select Python over RakNet with configured server identity and no digest
pin. The SDK's optional `raknet` extra and compatible native wheels are required.
Default `bbench bots fetch --module python` verifies the adapter wheel from this
fork's release before installation; a matching adapter release and SDK/native
wheels must be available. Use the explicit local-wheel development path in the
[bot guide](bots/README.md#python-installation) until then.

## Read the results

A session under `runs/<session-id>/` contains each run's raw files, `report.md`,
`summary.json` and CSV data. Regenerate the report with:

```shell
uv run bbench report runs/<session-id>
```

Each run records configuration, server and client identities, machine information,
phase boundaries and health. Invalid runs and crashes remain visible. Client
presence alone does not establish sustained input or completed chunk loading.
Python SDK transport-byte counters are unavailable for both transports; see the
bot accounting rules.

[Methodology](docs/methodology.md) defines workload and validity rules, including
our [conflict of interest](docs/methodology.md#conflict-of-interest): EndstoneMC
maintains this benchmark and develops Endstone. [Metrics](docs/metrics.md) defines
the units and limits of each measurement.

## Supported servers

Versions, downloads and runtime dependencies are declared in each adapter's
`server.toml`. See its README for platform restrictions and setup:

| Server | Adapter |
| --- | --- |
| Bedrock Dedicated Server | [BDS](servers/bds/) |
| Endstone | [Endstone](servers/endstone/) |
| LeviLamina — Windows only | [LeviLamina](servers/levilamina/) |
| PocketMine-MP — protocol 1001 | [PocketMine-MP](servers/pocketmine/) |
| Altay | [Altay](servers/altay/) |
| Nukkit | [Nukkit](servers/nukkit/) |
| Nukkit-MOT | [Nukkit-MOT](servers/nukkitmot/) |
| PowerNukkitX | [PowerNukkitX](servers/powernukkitx/) |
| Allay — protocol 1001 | [Allay](servers/allay/) |
| Dragonfly | [Dragonfly](servers/dragonfly/) |
| Chorus | [Chorus](servers/chorus/) |

Fetch and run commands report unavailable adapters rather than silently replacing
them. Protocol 1001 servers need their separate bot module. Some source-built
servers need additional toolchains, and upstream snapshot downloads can disappear;
retain artifacts when reproducibility requires them.

## Contributing

See [.github/CONTRIBUTING.md](.github/CONTRIBUTING.md) for development setup, tests,
adding a server and metric/schema versioning. Keep server-specific behavior in its
adapter and measurement rules shared across servers.

## Citation

Cite the repository and the harness version recorded in your runs:

```bibtex
@software{bedrock_bench,
  author = {{EndstoneMC}},
  title = {bedrock-bench: a load benchmark for Minecraft Bedrock servers},
  year = {2026},
  url = {https://github.com/EndstoneMC/bedrock-bench}
}
```

## License

[Apache-2.0](LICENSE); third-party notices are in [NOTICE](NOTICE).
Not affiliated with Mojang Studios or Microsoft. Minecraft and Bedrock are their
respective owners' trademarks.
