# graph-cloud-benchmark

A reproducible, resource-parity benchmark harness for graph databases, built
around the SNAP cit-HepTh citation network (27,770 nodes, 352,807 edges).

**Default scope: four self-hosted engines** — **Neo4j**, **Memgraph**,
**FalkorDB** and **ArangoDB** — each in a container capped at 1 vCPU and 2 GB,
measured one at a time. This is the tightest comparison the harness can make:
every target sits behind the same loopback path under identical caps, so the
network confound that dogs managed-vs-local benchmarks is absent by
construction.

The harness also supports two managed targets — **CognoDB Cloud** and **Neo4j
AuraDB Free**, the latter as a calibration anchor rather than a competitor.
Both are optional and are reported as *not configured* when their credentials
are absent. Adding either reintroduces the internet round trip, which is why
the anchor exists; see [`docs/methodology.md`](docs/methodology.md) §4.

> **This repository contains a harness, not results.** Numbers appear only
> after you run it against instances you control. Nothing here ships
> pre-computed measurements, and none should ever be added by hand — the
> report is generated from `results/raw/` and can be recomputed by anyone
> holding that file.

**Everything containerised runs in GitHub Codespaces.** The devcontainer is the
supported environment; no Docker installation on a local machine is expected or
required.

## Why another graph benchmark

Most published graph benchmarks are unreproducible, unfairly configured, or
both — usually by accident, occasionally not. This one is built so its
failure modes are visible:

- **Every engine gets identical query parameters**, generated once from a
  seeded RNG before any target is touched — at every concurrency level.
- **Every engine gets an equivalent index**, created before measurement and
  never timed.
- **Loads are verified against the server's own counts.** A partial load fails
  the target instead of producing suspiciously good latencies.
- **Row counts are cross-checked between engines**, per workload and per
  concurrency level. If two targets disagree on how many rows a workload
  returns, they were not answering the same question, and the report
  suppresses the speed comparison and says why.
- **Query text for all dialects lives in one file**, side by side, so an
  unfair translation is visible in review.
- **Unreachable and unconfigured targets are printed, not dropped.** A missing
  row reads as "did not compete"; the truth is usually "was not configured".
- **Enforced resource limits are measured, not assumed** — `make probe` reads
  the actual cgroup caps and fails if they disagree with the config.
- **Raw per-iteration timings are committed**, so anyone who dislikes our
  statistics can compute their own.

Read [`docs/methodology.md`](docs/methodology.md) before quoting anything from
a run — particularly §2 (what is not measured), §4 (the limits of resource
parity) and §10 (how this benchmark could still mislead you).

## Running it in Codespaces

1. Push this repository to GitHub.
2. **Code → Codespaces → Create codespace on main.** Take the default
   **2-core / 8 GB** machine; the setup is designed to fit it.
3. In the Codespace terminal:

```bash
make env                  # generate .env with passwords for the four containers
make smoke                # feasibility check: 1 iteration, proves the plumbing
make suite                # the real run, then merge + report
```

`make env` needs no external account: the four self-hosted engines are the
default scope, and their passwords are generated rather than chosen. Add
`--managed` to stub empty CognoDB and Aura blocks for filling in later.

A target with no credentials is reported as **not configured** in every table
rather than omitted, so a narrower run produces an honest report rather than a
silently smaller one.

`make smoke` and `make suite` bring up **one database container at a time**,
wait for it to report healthy, verify its enforced limits, measure it, and tear
it down before moving to the next. That is not just a memory workaround — three
idle databases holding page cache while a fourth is measured would be measuring
the host.

Each target writes its own raw file; `scripts/merge_runs.py` joins them and
refuses unless the runs are genuinely comparable (same dataset, seed, iteration
counts and schema version). The merged manifest records that the targets were
measured sequentially rather than simultaneously.

Useful variations:

```bash
make bench TARGET=memgraph                          # one target
make up SERVICE=memgraph && make wait SERVICE=memgraph
python scripts/run_benchmark.py --dry-run           # show the plan
python scripts/run_benchmark.py --workload point_lookup --iterations 500
python scripts/make_report.py --baseline neo4j-selfhosted
```

## What it measures

Eight workloads plus bulk load. Concurrency levels are per workload; the
cheap, high-frequency ones run at 1, 10 and 40 clients.

| Workload | Exercises | Concurrency |
|---|---|---|
| `point_lookup` | single-key index access | 1, 10, 40 |
| `one_hop` | one-step expansion from high out-degree papers | 1, 10, 40 |
| `two_hop` | two-step expansion with deduplication | 1 |
| `neighbourhood_3hop` | three-hop undirected reachability count | 1 |
| `shortest_path` | built-in shortest path between pre-validated pairs | 1 |
| `top_cited` | full scan and grouping over all edges | 1 |
| `date_filtered_top` | predicate filter plus incoming-edge counting | 1 |
| `mixed_read_write` | 90/10 read/write mix, counter updates | 1, 10, 40 |
| `ingest` | batched bulk load, reported as edges/second | — |

Reported per target: p50, p90, p95, p99 (nearest-rank), min/max/mean/stdev,
achieved throughput, rows returned, and load throughput. Throughput under
concurrency is computed from the measured phase's **wall clock**, never by
summing overlapping per-request latencies.

It does **not** measure durability, replication, failover, or behaviour at
scale beyond this dataset.

## Layout

```
benchmark/
  core/        config, timing, result schema, error taxonomy
  datasets/    cit-HepTh parsing and seeded parameter sampling
  databases/   one adapter per wire protocol (Bolt, RESP, HTTP/AQL)
  workloads/   all query dialects, side by side
  metrics/     nearest-rank percentiles and their caveats
  runners/     orchestration: reset, load, verify, warm up, measure, concurrency
  reporting/   summaries, tables, charts, cross-engine integrity checks
config/        what runs, how, and against what (no credentials)
infra/         resource-capped containers, started one at a time
scripts/       download_data, probe_limits, run_benchmark, merge_runs,
               make_report, scan_secrets, run_suite.sh
results/       raw/ per-iteration record, summary/ derived percentiles
```

Adapters are keyed by **protocol, not product**: CognoDB, Neo4j, Aura and
Memgraph all run through the same `BoltAdapter` with the same driver and the
same result-consumption path, so no vendor can be advantaged by a client-side
difference. Where engines genuinely diverge (index DDL, bulk delete, version
probes) the difference is isolated in one table and selected by a config
setting, not by target name.

## Before you trust a run

- **Confirm the parity assumption.** The CPU and memory figures in
  `config/databases.yaml` are the target this harness was written against, not
  measurements of what any vendor provisions. `make probe` verifies the
  container side; the managed side cannot be verified from outside at all.
- **If you add CognoDB, confirm its dialect first.** The config assumes
  Neo4j-compatible Cypher and Bolt 5.x. If its DDL or bulk-delete syntax
  differs, change the `flavour` setting rather than the adapter — and if the
  difference is larger than that, the comparison needs revisiting before it is
  published.
- **If you add any managed target, add Aura too.** Aura and self-hosted Neo4j
  run the same engine, so the gap between them is the managed-cloud round trip
  rather than the database. Without that anchor a slow managed result and a
  slow engine are indistinguishable.
- **Run it more than once.** Free-tier instances share hardware. A gap smaller
  than the spread between repeat runs is not a finding.

## Development

```bash
make test     # unit tests, no database required
make lint     # ruff check + format check
make secrets  # credential scan
make check    # all three, as CI runs them
```

The unit suite runs without any database and without the driver packages
installed — every adapter imports its driver lazily — and includes an
end-to-end test of the fairness machinery against an in-memory fake engine:
identical parameters, discarded warmups, verified loads, flagged
disagreements, and the concurrency partition.

CI deliberately never runs the measured benchmark: shared runners have no
resource guarantees, and a latency measured there would describe GitHub's
fleet rather than any database.

## Licence

MIT. See [LICENSE](LICENSE). The cit-HepTh dataset is redistributed by SNAP
under its own terms and is downloaded rather than vendored.
