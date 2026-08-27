# Methodology

This document exists so that a reader who distrusts the numbers can find out
exactly how they were produced, and so that a reader who trusts them too much
can find out what they do not cover.

Nothing here is a result. Results live in `results/raw/`, and the report
generated from them lives alongside as `docs/report-<run_id>.md`. If this
repository contains no such files, no run has been performed yet.

---

## 1. What is measured

Single-client, sequential query latency against a graph loaded from a fixed
public dataset, plus the wall time of the bulk load itself.

Each measured value is the time from issuing a statement to having consumed
its final row, measured client-side with `time.perf_counter_ns`. It therefore
includes network round-trip time, driver serialisation, server planning and
server execution. That is deliberate: it is the latency an application
actually experiences, and separating out "server time" would require trusting
each engine's own profiler to define the boundary the same way, which they do
not.

## 2. What is *not* measured

Stating this plainly matters more than the numbers do.

- **Concurrency beyond 40 clients.** Workloads are measured at 1, 10 and 40
  concurrent clients where that is cheap enough to be meaningful. Higher
  levels, connection-pool tuning and long-running saturation tests are out of
  scope. `throughput_qps` is measured from the wall clock of the concurrent
  phase, so it is a real rate - but it is the rate this client achieved
  against this instance, not a capacity limit.
- **Sustained write throughput.** `mixed_read_write` interleaves counter
  updates with reads at a 90/10 ratio. That exercises the write path under a
  read-heavy mix; it does not measure bulk write throughput, contention
  between writers, or lock behaviour.
- **Durability, replication, backup, failover, security.** All of these are
  production concerns that a latency harness has nothing to say about.
- **Scale.** cit-HepTh is a small graph, chosen so it fits in a free tier. An
  engine that wins here may lose at a hundred times the size, and nothing in
  these results predicts which.
- **Cost.** Free tiers are compared with free tiers. That is a starting point
  for a purchasing decision, not the decision.

## 3. Dataset

The SNAP cit-HepTh arXiv citation network: 27,770 papers, 352,807 citations,
plus a companion file of publication dates.

- Both files are pinned by SHA-256 (`benchmark/datasets/cit_hepth.py`). A
  changed upstream file halts the run rather than silently redefining the
  experiment.
- 39 self-citations are dropped, so 352,768 edges are loaded. The count is
  reported so the discrepancy against the published figure is explained
  rather than mysterious.
- Node ids are arXiv identifiers with leading zeros stripped; the dates file
  keeps the padding and marks cross-listed papers with a `11` prefix. Both are
  normalised on load.
- **Only ~41% of graph nodes carry a publication date.** The two source files
  cover overlapping but different paper sets. This is a property of the data,
  not a parsing bug; it is measured at load time rather than assumed, and the
  date-filtered workload is scoped accordingly.

## 4. Resource parity, and its limits

Self-hosted engines run in containers capped by `infra/docker-compose.yml`,
with the caps recorded in `config/databases.yaml` so the report can state what
parity was claimed.

Four honest caveats:

1. **The managed tiers cannot be verified from outside.** What a vendor
   provisions for a free instance is what they say it is. The container caps
   are set to match the *advertised* allocation. Confirm those figures against
   the tiers as sold today before quoting any result; the values in
   `config/databases.yaml` are a parity target, not a measurement.
2. **A container cap is not the same shape as a cloud allocation.** Identical
   CPU and memory limits still differ in storage class, network path and
   noisy-neighbour exposure.
3. **Targets are measured one at a time, never together.** The configured
   caps total 4 vCPU and 8 GB, which does not fit on the 2-core Codespace at
   all. Even where it would fit, three idle databases holding page cache while
   a fourth is measured means measuring the host. Each target is brought up,
   verified, measured and torn down in turn, and `scripts/merge_runs.py` joins
   the results only if the runs are genuinely comparable. The consequence,
   recorded in every merged manifest, is that the targets were measured at
   different moments - so a slow managed target may have met a busier network
   than a fast one did.

4. **Managed instances are reached over the internet; containers are reached
   over loopback.** This is the single largest confound in the whole
   comparison, and it is why Neo4j AuraDB Free is included. Aura and the
   self-hosted Neo4j target run the *same engine*, so the gap between them
   measures the managed-cloud round trip rather than the database. Any
   managed-versus-container comparison should be read through that gap first.
   Without that anchor, a slow managed result and a slow engine are
   indistinguishable.

## 5. Query equivalence

The query text for every workload lives in one file,
`benchmark/workloads/queries.py`, with each dialect next to the others so
divergence is visible in review rather than buried per adapter.

Every workload declares how comparable its variants really are, and the
declaration is carried into the report:

| Level | Meaning |
|---|---|
| `identical` | the same text runs on every engine that reached the workload |
| `idiomatic` | a faithful translation using each engine's natural construct |
| `loose` | same answer, materially different means; do not quote the ratio alone |

Exactly one workload is `loose`: `neighbourhood_3hop`. Cypher enumerates paths
and deduplicates with `count(DISTINCT ...)`; AQL prunes during the walk with
`uniqueVertices: 'global'`. Both return the size of the three-hop
neighbourhood, but they do different amounts of work to get there, so that row
compares engine-plus-optimiser rather than traversal speed.

ArangoDB is the only non-Cypher target. Its AQL is hand-written, so every
ArangoDB row carries translation risk the Cypher rows do not. Modelling
choices there (paper id as `_key` for primary-index lookups, an indexed
integer `pid` alongside) were made to avoid handicapping it; they are
documented in `benchmark/databases/arangodb.py` and are open to challenge.

### Dialect mapping

Four engines, three dialects. Most statements are shared; the table records
every place they are not, and why.

| Engine | Dialect chain | Diverges on |
|---|---|---|
| Neo4j, Aura, CognoDB | `cypher` | - |
| Memgraph | `cypher_memgraph` -> `cypher` | `shortest_path` |
| FalkorDB | `cypher_falkordb` -> `cypher` | `shortest_path` |
| ArangoDB | `aql` | everything |

A missing specialised statement falls back to generic Cypher. That is
convenient and it is exactly how the smoke run found a bug: Memgraph was
silently handed Neo4j's `shortestPath()`, which it does not implement.

**`shortest_path` on Memgraph.** Memgraph has no `shortestPath()` function.
Its documented equivalent is a BFS expansion, which by definition returns one
shortest path:

```cypher
-- Neo4j / Aura / CognoDB / FalkorDB
MATCH (a:Paper {id: $source}), (b:Paper {id: $target})
MATCH path = shortestPath((a)-[:CITES*..8]-(b))
RETURN length(path) AS hops

-- Memgraph
MATCH path = (a:Paper {id: $source})-[:CITES *BFS ..8]-(b:Paper {id: $target})
RETURN size(relationships(path)) AS hops
```

Both are the engine's own shortest-path operator, both are bounded at 8 hops,
both filter to `:CITES`, and both return a single row holding the hop count.
This is `idiomatic` equivalence, not `identical`: Memgraph is not being asked
an easier question, it is being asked the same question in the only way it
accepts. Writing a manual traversal to emulate `shortestPath()` would have
been worse - it would measure our emulation rather than the engine.

FalkorDB, despite not being a Bolt server, *does* provide `shortestPath()`.
Its restrictions - endpoints resolved before the call, no property filter
inside the pattern - are why the shared statement resolves `a` and `b` in a
separate `MATCH` rather than inlining them. Neo4j is equally happy with that
form, so one statement serves both.

**`shortest_path` on FalkorDB.** FalkorDB has `shortestPath()`, but its
planner rejects the undirected variable-length pattern this workload uses. Its
native equivalent is the `algo.SPpaths` procedure, which takes direction as an
explicit argument:

```cypher
MATCH (a:Paper {id: $source}), (b:Paper {id: $target})
CALL algo.SPpaths({sourceNode: a, targetNode: b, relTypes: ['CITES'],
                   relDirection: 'both', maxLen: 8, pathCount: 1})
YIELD path
RETURN length(path) AS hops
```

Same relationship type, same 8-hop bound, same undirected traversal, same
single-row hop count. A native procedure is a fair substitute only when it
answers the identical question, and a test asserts each of those four
properties rather than trusting the prose.

**`neighbourhood_3hop` on FalkorDB: a documented failure, not a workaround.**
FalkorDB times out on the undirected three-hop neighbourhood count under the
1 vCPU / 2 GB cap. It has an `algo.bfs` procedure that would complete easily,
and swapping it in would turn a red cell green — but `algo.bfs(start, depth,
relType)` takes no direction argument and follows outgoing edges only. It
would compute the *directed* three-hop neighbourhood, which is a strictly
smaller set and a different question. A benchmark that quietly answers an
easier question on one engine is worse than one with a gap in it, so the
timeout stands and is reported as a timeout.

Two honest options remain, and both are the reader's to take rather than
ours: accept the gap and read the row as "FalkorDB could not complete this
formulation at this cap", or lower the hop bound **for every engine** and
re-run, which measures something completable at the cost of measuring
something shallower. What is not available is lowering it for FalkorDB alone.

**Index verification.** Creating the index and *having* the index are
different claims. Memgraph rejects Neo4j's constraint syntax and treats a
repeated index creation as an error, so its flavour has to tolerate DDL
failures to stay portable - which on its own would let a run proceed
unindexed. Every adapter therefore confirms the index separately after schema
setup (`SHOW INDEXES`, `SHOW INDEX INFO`, `CALL db.indexes()`, or the
collection's index list), and the result is carried into the report as an
`indexed` column. A `NO` there means that target was measured without the
index every other target had, and its read rows are not comparable.

**Schema parity is enforced.** Every engine gets an equivalent index on the
paper id before any measurement, and index creation is never timed.

## 6. Parameters

Query parameters are generated **once per run**, from a seeded RNG, before any
target is touched, and the identical list is replayed against every engine in
the identical order. No engine draws its own sample.

- Point lookups use 100 randomly sampled paper ids.
- Expansion workloads start from the 200 highest out-degree papers. A random
  paper here cites about a dozen others, which would time the round trip
  rather than the traversal. The choice is applied identically everywhere and
  is stated here, which is what distinguishes it from cherry-picking.
- Shortest-path pairs are pre-validated by breadth-first search over the
  undirected projection, so every pair is reachable. Otherwise an engine that
  gives up quickly on a disconnected pair would look fast for doing no work.
- Changing `seed` changes the questions asked. A run with a different seed is
  a different experiment.

**Under concurrency the same list is partitioned, not regenerated.** Worker w
takes positions w, w+N, w+2N, ..., so the union of what N clients ask is
exactly the multiset a single client would have asked, in the same relative
order. The measured phase continues the parameter index past the warmup, so
level 1 and level 40 ask the same questions - an earlier revision restarted
the index at zero for the concurrent path, which quietly made the levels
incomparable, and a test now pins the behaviour.

**The read/write mix is fixed by the seed, not by the engine.** The operation
at each position of `mixed_read_write` is drawn once and replayed, so every
target performs the same operation in the same order. A mix re-randomised per
engine would hand one of them a different ratio of cheap reads to expensive
writes and then call the difference performance. The write increments a
counter property and never touches graph structure, and the workload is
always scheduled last, so it cannot change what any other measurement saw.

## 7. Statistics

- **Warmup:** the first N iterations are executed and excluded. They are still
  written to `results/raw` with a negative index, so the exclusion can be
  audited rather than merely trusted.
- **Percentiles:** nearest-rank, no interpolation. Every reported percentile
  is a latency that was actually observed. This is the main reason two honest
  tools disagree on p99, so it is stated rather than left implicit.
- **Sample size:** nearest-rank p99 needs at least 100 samples to be
  distinguishable from the maximum. The default is 100. Below that the harness
  still reports a p99 but flags it, and the report marks it with a dagger.
- **Failed iterations** are excluded from the statistics and counted
  separately. A timeout is never recorded as a slow success.

## 8. Integrity checks

Two checks run on every benchmark, because the cheapest way for a comparison
to be wrong is for one engine to be answering an easier question.

1. **Load verification.** After ingest, each server is asked how many nodes
   and relationships it holds. A mismatch marks the target's load as failed
   and annotates the run: a partially loaded graph produces excellent latency
   numbers, and without this check they would be published as a win.
2. **Cross-engine row agreement.** Every workload is written so all dialects
   return the same row count for the same parameters. Row counts are compared
   per iteration across targets; disagreement is reported, and the affected
   workload has its relative-speed column suppressed. A ratio between two
   different questions is not a speedup.

Neither check can run meaningfully with a single target, and the summary says
so explicitly rather than reporting a clean bill of health.

## 9. Reporting conventions

- A target that was not configured, could not be reached, or failed a workload
  gets an explicit row saying so. It is never omitted, because a missing row
  in a comparison table reads as "did not compete" rather than "was not
  measured".
- Relative numbers default to the first target that produced results, not to
  any nominated product. A table whose ratios are all computed against one
  vendor flatters that vendor by construction.
- Charts use one colour for all targets, with identity carried by the axis
  label. Painting the subject of the benchmark in its own colour directs the
  eye, and in a vendor comparison that is worth avoiding deliberately.
- Charts are small multiples on linear axes, never one grouped chart on a log
  axis: a bar on a log scale has a length that is no longer proportional to
  its value.

## 10. Known ways this benchmark could still mislead

Listed because a limitations section that only contains flattering
limitations is not a limitations section.

- **The workload set is a choice.** Seven read patterns on one citation graph.
  A different seven could reorder the results, and no set of seven is neutral.
- **Free tiers are not comparable products.** Vendors position them
  differently - some as evaluation sandboxes, some as genuine small
  deployments. Equal CPU and memory does not make equal intent.
- **Version skew.** Each engine is pinned, but the pins are a snapshot. The
  server versions actually contacted are recorded in the run manifest; check
  them before comparing two runs.
- **Client location matters enormously** for the managed targets and not at
  all for the containers. A run from a different region produces different
  managed numbers with no change in the databases.
- **The `loose` workload should not be quoted on its own**, and the AQL
  translations should be reviewed by someone who writes AQL professionally
  before any ArangoDB row is cited.
- **A single run is an anecdote.** Free-tier instances share hardware and
  drift over hours. Repeat runs at different times before believing a gap
  smaller than the spread between them.

## 11. Reproducing a run

```bash
cp .env.example .env          # fill in credentials for the targets you have
make install
make data                     # download + checksum-verify the dataset
make up && make wait          # start the capped containers
make status                   # confirm the caps the daemon actually applied
make bench                    # measure
make report                   # tables and charts from the raw record
```

Every target is optional. Configure one and only that one is measured; the
rest are reported as not configured. The report is regenerated from
`results/raw/` alone, so any derived number can be recomputed by anyone
holding that file.
