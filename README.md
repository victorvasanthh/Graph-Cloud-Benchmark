# Graph-Cloud-Benchmark

A reproducible, resource-parity benchmark harness for graph databases, built
around the SNAP cit-HepTh citation network.

**Dataset:** 27,770 nodes. The source file contains **352,807** edges; **39 are
self-loops** and are dropped at parse time, so every platform is loaded with
**352,768 edges**. The count is verified against the server after each load.

## What was actually measured

**Four targets completed the full 100-iteration benchmark** and are the only
ones in the comparison tables:

| Target | Status |
|---|---|
| **Memgraph** (container) | complete — all 8 workloads |
| **ArangoDB** (container) | complete — all 8 workloads |
| **Neo4j 5** (container, self-hosted) | complete — all 8 workloads |
| **Neo4j AuraDB Free** (managed) | complete — all 8 workloads |

**Two targets did not produce a comparable result, and neither is in any
results table:**

| Target | What happened |
|---|---|
| **FalkorDB** (container) | **Not benchmarked.** A 100-iteration run was in progress when the Codespace restarted; the process was killed before writing any result file. An earlier smoke run reached it successfully, so this is an *interrupted measurement*, not an engine that could not be benchmarked. No FalkorDB timing appears anywhere in this repository. |
| **CognoDB Cloud** (managed) | **Not included in the full comparison.** The instance was successfully connected and loaded, but the full 100-iteration benchmark was not completed: several workloads lost the connection or failed during the run on the free tier. No CognoDB performance result is used in the final comparison. *(Tables show `not reachable` because the connection at the start of the final run did not authenticate; see [Evidence for CognoDB](#evidence-for-cognodb).)* |

Nothing in this report says whether FalkorDB or CognoDB would have been faster
or slower. Their absence is a gap in this benchmark, not a finding about them.

> **Results are generated, never hand-written.** Every number below is derived
> from `results/raw/final-combined.json` by `scripts/make_report.py`, and the
> report's Limitations and Conclusion sections are generated from that same
> record so they cannot drift from the data they describe.

**Everything containerised runs in GitHub Codespaces.** The devcontainer is the
supported environment; no Docker installation on a local machine is expected or
required.

### Evidence for CognoDB

Because smoke-run artifacts are gitignored, the successful connection and load
are **not** present as result data in this repository. What is verifiable here:

- **Connection and load succeeded** during diagnostics. The docstring of
  [`tests/test_schema_diagnostics.py`](tests/test_schema_diagnostics.py) records
  that CognoDB loaded 27,770 nodes and 352,768 edges, and
  [`benchmark/databases/bolt.py`](benchmark/databases/bolt.py) contains a
  CognoDB-specific Cypher flavour that exists only because a live instance
  rejected `CALL { ... } IN TRANSACTIONS` at parse position 42 — code written
  against observed behaviour, not speculation.
- **Several workloads completed, several lost the connection.** Point lookup,
  one-hop and two-hop returned; the three-hop neighbourhood died after ~25
  seconds, and the workloads after it could not re-establish a connection.
  `scripts/probe_workloads.py` exists to test each workload on a fresh
  connection for exactly this reason.
- **The index could never be confirmed**, which is why no CognoDB read timing
  would have been publishable even had the run completed — an engine reading
  without the index the others had is answering an easier question.
- **The final run did not authenticate.** `results/raw/final-combined.json`
  records 15 runs, all `unavailable`, with a
  `Neo.ClientError.Security.Unauthorized` error. That is why every table cell
  reads `not reachable`.

**What is deliberately not claimed:** the cause of the mid-run connection
losses. The client saw the socket close; nothing in this repository establishes
whether that was a memory limit, a query timeout, a proxy, or something else,
and server-side logs were not available.

## ⚠️ Deviation from the assignment brief

**The resource-parity requirement was not matched.** This is the most
significant weakness in this submission and it is stated here rather than left
for a reader to find.

| | Brief's reference tier | What this benchmark used |
|---|---|---|
| vCPU | 0.5 (burstable) | **1.0** |
| RAM | 256 MB | **2 GB** |
| Disk | 1 GB | not capped |

The brief specifies the CognoDB free tier as *burstable 0.5 vCPU, 256 MB RAM,
1 GB disk* and asks that every platform be run on equivalent resources. The
containers here were capped at 1 vCPU and 2 GB — roughly **twice the CPU and
eight times the memory** — and Neo4j AuraDB Free is a vendor-defined tier that
is not 256 MB either.

**What this costs.** Absolute cross-platform numbers are not a fair
free-tier-versus-free-tier comparison, and the brief is explicit that comparing
databases on unequal resources is a methodology error. The in-memory engines
(Memgraph) benefit most from the extra headroom, since 256 MB would force very
different behaviour on a 352,768-edge graph than 2 GB does.

**What still holds.** The four measured targets were capped **identically to
each other**, ran the same dataset with the same seeded parameters in the same
order, one at a time, with indexes verified on every platform. Comparisons
*among those four* remain internally consistent; what cannot be claimed is that
those figures represent behaviour at the brief's reference tier.

**Why it happened, plainly:** the caps were chosen early from the tier figures
recorded in `config/databases.yaml`, which were written as an assumption before
the brief's exact numbers were applied, and the parity assumption was never
revisited before the runs. Re-running at 0.5 vCPU / 256 MB is the correct fix
and was not possible within the remaining time.

The caps are enforced and verified rather than merely claimed — `make probe`
reads the cgroup limits from inside each container, because a nested Docker
daemon can accept `--memory` and silently ignore it.

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

If creation ever fails during the feature install, rebuild with
**Ctrl/Cmd-Shift-P → Codespaces: Rebuild Container** (use *Full Rebuild* to
bypass the image cache). The devcontainer builds from a local
[`Dockerfile`](.devcontainer/Dockerfile) specifically so that the image's stale
Yarn APT source is removed before `docker-in-docker` runs `apt-get update` —
without that, the install dies on `NO_PUBKEY 62D54FD4003F6525`.

```bash
make doctor               # confirm docker, compose, and enforced cgroup limits
make env                  # generate .env with passwords for the four containers
make smoke                # feasibility check: 1 iteration, proves the plumbing
make suite                # the real run, then merge + report
```

`make doctor` runs automatically at Codespace creation. It checks more than
`docker --version`: it starts a throwaway container under `--cpus` and
`--memory` and reads the cgroup files back, because a nested Docker daemon can
report healthy while silently ignoring both. A parity benchmark run under caps
that were never applied is worthless, and looks identical to a good one.

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

## Results

Full report: **[docs/report-merged-20260828T103536Z.md](docs/report-merged-20260828T103536Z.md)**
Raw per-iteration timings: `results/raw/final-combined.json`

100 measured iterations after 5 warmup, seed 20260827, one target at a time,
each container capped at 1 vCPU / 2 GB. Every engine below loaded the same
27,770 nodes and 352,768 edges with counts verified by the server and the
`Paper(id)` index confirmed.

**Median latency, milliseconds (lower is better)**

| workload | Memgraph | ArangoDB | Neo4j (local) | Aura Free |
|---|---|---|---|---|
| point_lookup | **0.81** | 1.48 | 3.31 | 64.92 |
| one_hop | 3.52 | **2.96** | 11.24 | 67.68 |
| two_hop | **23.24** | 32.96 | 37.36 | 151.35 |
| neighbourhood_3hop | 374.00 | 183.04 | **40.44** | 94.95 |
| shortest_path | 84.99 | **2.20** | 2.82 | 65.59 |
| top_cited | 151.04 | **95.42** | 124.16 | 263.12 |
| date_filtered_top | **14.60** | 16.89 | 18.15 | 80.40 |
| mixed_read_write | **1.22** | 1.79 | 2.34 | 65.46 |
| bulk load | **7.0 s** | 7.7 s | 28.6 s | 32.7 s |

### What the numbers show

**The largest single effect is not a database at all.** Aura Free and the
self-hosted Neo4j container run the same engine, yet the point lookup differs
by roughly twenty times - 64.92 ms against 3.31 ms. Aura's floor sits near
65 ms on every cheap workload, which is the shape of a network round trip
rather than of query execution: the four cheapest workloads all land within a
few milliseconds of each other on Aura while spanning 0.81-3.52 ms locally.
That gap is why Aura is in this benchmark at all. **Read every
managed-versus-container comparison through it**, or a slow network path will
be mistaken for a slow database.

**In-memory storage shows up exactly where you would expect it, and not
elsewhere.** Memgraph wins the small, latency-bound workloads - point lookup,
the mixed read/write, date-filtered - and loads the dataset four times faster
than Neo4j (7.0 s against 28.6 s). But it is the *slowest* on the three-hop
neighbourhood at 374 ms against Neo4j's 40.44 ms. Being memory-resident helps
when the work is dominated by per-operation overhead; it does not help when
the work is dominated by how many paths the planner enumerates.

**`neighbourhood_3hop` is the one row not to quote on its own.** It is marked
`loose` equivalence in the report because the engines are allowed to reach the
same answer by different means: ArangoDB prunes with `uniqueVertices: 'global'`
while the Cypher engines enumerate paths and deduplicate. Neo4j leading here
measures planner strategy as much as traversal speed.

**ArangoDB is the most consistent performer and the hardest to interpret.** It
is fastest or near-fastest on six of eight workloads and comfortably wins
shortest path (2.20 ms against Memgraph's 84.99 ms). It is also the only
non-Cypher engine, so every one of its queries is a hand-written AQL
translation. Its rows carry translation risk the Cypher rows do not, and that
is a caveat about this benchmark rather than a hedge about the engine.

**Concurrency behaves as a saturation curve, not a speedup.** On point lookup,
Memgraph goes 967 → 1,285 → 943 requests/second across 1, 10 and 40 clients
while its p50 rises 0.81 → 5.59 → 7.80 ms: throughput peaks around ten clients
and then falls as latency grows. Aura moves the other way - 15 → 153 → 371
requests/second with p50 almost flat - because its cost is round-trip latency,
which parallelism hides. A single capped vCPU is the ceiling in one case and
the network is the ceiling in the other.

### What these numbers do not support

They describe four engines on one 27,770-node graph, at one size, under one set
of caps, from one client, on one day. They do not establish that any engine is
faster in general. **FalkorDB is absent** - its run was interrupted by a
Codespace restart, not excluded - and **CognoDB Cloud never completed a full run**
because authentication failed. Both are disclosed in the report's Limitations
section, and neither absence implies anything about those engines.

The report's own Limitations and Conclusion sections are generated from the run
record rather than written by hand, so they cannot drift from the data they
describe.

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
