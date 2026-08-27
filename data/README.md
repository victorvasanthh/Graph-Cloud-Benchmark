# data/

The dataset is **downloaded, not vendored**. `.gitignore` excludes everything
here except this file and `.gitkeep`.

```bash
python scripts/download_data.py      # or: make data
```

## Files

| File | Contents |
|---|---|
| `cit-HepTh.txt.gz` | 352,807 directed citation edges over 27,770 papers |
| `cit-HepTh-dates.txt.gz` | publication dates, keyed by arXiv identifier |

Source: <https://snap.stanford.edu/data/cit-HepTh.html>

## Integrity

Both files are pinned by SHA-256 in `benchmark/datasets/cit_hepth.py` and
verified on every load. A mismatch aborts the run rather than benchmarking
against unknown input — a benchmark whose input silently changed is not
reproducible, and the failure is worth more than the convenience.

`download_data.py` will not overwrite a file whose checksum does not match. It
reports the mismatch and stops, so an upstream change is something you decide
about rather than something that happens to you.

## Quirks handled at load time

Both are properties of the source data, not of this harness:

- **Leading zeros.** SNAP strips them from node ids in the edge list but the
  dates file keeps them, so paper `0001001` appears as `1001` on one side.
  Both are parsed as integers.
- **Cross-listed papers** carry a `11` prefix on the true id in the dates
  file, as its own header comment documents. The prefix is folded back.
- **Partial date coverage.** Even after both fixes, only about 41% of graph
  nodes have a publication date: the two files cover overlapping but different
  paper sets. The figure is measured on load and reported, and the
  date-filtered workload is scoped to nodes that have one.
- **Self-citations.** 39 papers cite themselves, an artefact of the id
  remapping. They are dropped, leaving 352,768 edges loaded, and the count is
  reported so the difference from the published figure is explained.
