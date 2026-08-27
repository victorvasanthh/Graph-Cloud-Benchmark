"""Charts, rendered to PNG with matplotlib.

Three presentation choices, each made to keep the picture from claiming more
than the numbers do:

**Small multiples, one panel per workload, each with its own linear x-axis.**
The obvious alternative - a single grouped bar chart with a log x-axis - is
not usable here. Latencies across these workloads span three orders of
magnitude, and a bar on a log axis has a length that is no longer proportional
to its value, so the eye reads a 10x gap as a modest one. Separate linear
panels keep every bar honest at the cost of not being able to compare across
panels, which is a comparison nobody should be making anyway.

**One colour for every target, identity carried by the axis label.** Painting
the subject of the benchmark in its own colour is a quiet way of directing the
eye, and in a vendor comparison that is worth avoiding on purpose. With the
target names written next to their bars, colour has no work left to do, so it
does none.

**Failures are drawn, not dropped.** A target that could not run a workload
gets a zero-length bar annotated with the reason. Removing the row would leave
a chart that looks like a clean sweep.
"""

from __future__ import annotations

import math
from pathlib import Path
from typing import Any

# Categorical slot 1 from the validated reference palette, used as a single
# series colour. Slot 2 marks nothing here; see the module docstring.
BAR_COLOUR = "#2a78d6"
SURFACE = "#fcfcfb"
TEXT_PRIMARY = "#0b0b0b"
TEXT_SECONDARY = "#52514e"
GRID = "#dedcd6"
FAILED_TEXT = "#e34948"

STATUS_TEXT = {
    "unavailable": "not reachable",
    "unsupported": "n/a",
    "failed": "failed",
}


def _format_ms(value: float) -> str:
    """Enough decimals to distinguish the bars, never more.

    A fixed two-decimal format collapses every sub-10-microsecond result to
    "0.00", which reads as "no time at all" rather than "too fast for this
    format". The precision follows the magnitude instead.
    """
    if value >= 100:
        return f"{value:,.0f}"
    if value >= 1:
        return f"{value:,.2f}"
    if value > 0:
        return f"{value:,.4f}".rstrip("0").rstrip(".") or "0"
    return "0"


def _configure(matplotlib_module: Any) -> None:
    matplotlib_module.rcParams.update(
        {
            "figure.facecolor": SURFACE,
            "axes.facecolor": SURFACE,
            "savefig.facecolor": SURFACE,
            "axes.edgecolor": GRID,
            "axes.labelcolor": TEXT_SECONDARY,
            "text.color": TEXT_PRIMARY,
            "xtick.color": TEXT_SECONDARY,
            "ytick.color": TEXT_PRIMARY,
            "font.size": 9,
        }
    )


def render_latency_panels(summary: dict[str, Any], output: Path) -> Path:
    """A panel per workload, targets sorted fastest first within each panel."""
    import matplotlib

    matplotlib.use("Agg")  # No display in CI, and none wanted anywhere.
    import matplotlib.pyplot as plt

    _configure(matplotlib)

    workloads = [name for name in sorted(summary["workloads"]) if name != "ingest"]
    if not workloads:
        raise ValueError("summary contains no read workloads to chart")

    columns = 2
    rows = math.ceil(len(workloads) / columns)
    figure, axes = plt.subplots(
        rows, columns, figsize=(11, 2.4 * rows + 1), squeeze=False, constrained_layout=True
    )

    for position, workload in enumerate(workloads):
        axis = axes[position // columns][position % columns]
        _draw_workload_panel(axis, workload, summary["workloads"][workload])

    for empty in range(len(workloads), rows * columns):
        axes[empty // columns][empty % columns].axis("off")

    manifest = summary["manifest"]
    figure.suptitle(
        f"Median query latency by workload - {manifest['dataset']}, "
        f"{manifest['measured_iterations']} measured iterations",
        fontsize=12,
        color=TEXT_PRIMARY,
    )

    output.parent.mkdir(parents=True, exist_ok=True)
    figure.savefig(output, dpi=160)
    plt.close(figure)
    return output


def _draw_workload_panel(axis: Any, workload: str, entry: dict[str, Any]) -> None:
    ok: list[tuple[str, float]] = []
    failed: list[tuple[str, str]] = []
    for target, record in entry["targets"].items():
        if record.get("status") == "ok":
            ok.append((target, float(record["p50_ms"])))
        else:
            failed.append((target, STATUS_TEXT.get(record.get("status", ""), "no result")))

    ok.sort(key=lambda pair: pair[1])
    failed.sort()
    labels = [name for name, _ in ok] + [name for name, _ in failed]
    values = [value for _, value in ok] + [0.0 for _ in failed]

    positions = range(len(labels))
    axis.barh(list(positions), values, color=BAR_COLOUR, height=0.6)
    axis.set_yticks(list(positions), labels)
    axis.invert_yaxis()
    axis.set_xlabel("p50 latency (ms)")
    axis.set_title(workload, loc="left", color=TEXT_PRIMARY, fontsize=10)

    axis.grid(axis="x", color=GRID, linewidth=0.6)
    axis.set_axisbelow(True)
    for side in ("top", "right", "left"):
        axis.spines[side].set_visible(False)

    headroom = max(values) * 1.25 if any(values) else 1.0
    axis.set_xlim(0, headroom)

    for index, (_, value) in enumerate(ok):
        axis.text(
            value + headroom * 0.02,
            index,
            _format_ms(value),
            va="center",
            fontsize=8,
            color=TEXT_SECONDARY,
        )
    for offset, (_, reason) in enumerate(failed):
        axis.text(
            headroom * 0.02,
            len(ok) + offset,
            reason,
            va="center",
            fontsize=8,
            color=FAILED_TEXT,
            style="italic",
        )

    if entry.get("equivalence") == "loose":
        axis.text(
            1.0,
            1.02,
            "loose equivalence",
            transform=axis.transAxes,
            ha="right",
            fontsize=7,
            color=FAILED_TEXT,
        )


def render_ingest_chart(summary: dict[str, Any], output: Path) -> Path | None:
    """Bulk-load wall time per target. Returns None when nothing loaded."""
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    _configure(matplotlib)

    entry = summary["workloads"].get("ingest")
    if not entry:
        return None

    loaded = [
        (target, float(record["p50_ms"]) / 1000.0)
        for target, record in entry["targets"].items()
        if record.get("status") == "ok"
    ]
    if not loaded:
        return None
    loaded.sort(key=lambda pair: pair[1])

    figure, axis = plt.subplots(figsize=(7, 0.5 * len(loaded) + 1.6), constrained_layout=True)
    positions = range(len(loaded))
    axis.barh(list(positions), [seconds for _, seconds in loaded], color=BAR_COLOUR, height=0.55)
    axis.set_yticks(list(positions), [name for name, _ in loaded])
    axis.invert_yaxis()
    axis.set_xlabel("bulk load wall time (s)")
    manifest = summary["manifest"]
    axis.set_title(
        f"Loading {manifest['dataset_nodes']:,} nodes and {manifest['dataset_edges']:,} edges",
        loc="left",
        fontsize=11,
    )
    axis.grid(axis="x", color=GRID, linewidth=0.6)
    axis.set_axisbelow(True)
    for side in ("top", "right", "left"):
        axis.spines[side].set_visible(False)

    headroom = max(seconds for _, seconds in loaded) * 1.2
    axis.set_xlim(0, headroom)
    for index, (_, seconds) in enumerate(loaded):
        axis.text(
            seconds + headroom * 0.02,
            index,
            f"{seconds:,.1f}s",
            va="center",
            fontsize=8,
            color=TEXT_SECONDARY,
        )

    output.parent.mkdir(parents=True, exist_ok=True)
    figure.savefig(output, dpi=160)
    plt.close(figure)
    return output
