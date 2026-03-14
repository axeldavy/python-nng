"""Reporting utilities: Markdown tables and matplotlib/seaborn plots."""

from __future__ import annotations

import io
import math
from pathlib import Path
from typing import Any

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import matplotlib.patches as mpatches
import numpy as np
import seaborn as sns

from ._core.stats import Stats

# ---------------------------------------------------------------------------
# Types
# ---------------------------------------------------------------------------
# results structure:
#   results[metric][transport][msg_size][competitor] = Stats
#   metric in {"latency", "bandwidth", "ops"}
Results = dict[str, dict[str, dict[int, dict[str, Stats]]]]

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _fmt_size(n: int) -> str:
    """Human-readable byte count: '8 B', '4 KB', '1 MB'."""
    if n < 1024:
        return f"{n} B"
    if n < 1024 ** 2:
        return f"{n // 1024} KB"
    return f"{n // (1024 ** 2)} MB"


def _competitors_ordered(data: dict[str, Stats]) -> list[str]:
    """Return competitor names in deterministic order."""
    prefer = [
        "c_nng",
        "nng_sync", "nng_async", "nng_async_old",
        "zmq_sync", "zmq_async",
        "pynng_sync", "pynng_async",
    ]
    keys = list(data.keys())
    ordered = [k for k in prefer if k in keys]
    ordered += [k for k in keys if k not in ordered]
    return ordered


# ---------------------------------------------------------------------------
# Markdown reporter
# ---------------------------------------------------------------------------

class MarkdownReporter:

    # ---- Latency -----------------------------------------------------------

    @staticmethod
    def latency_table(
        results: Results,
        transport: str,
        msg_size: int,
    ) -> str:
        """Build a Markdown table for latency (µs) of all competitors."""
        data: dict[str, Stats] = results.get("latency", {}).get(transport, {}).get(msg_size, {})
        if not data:
            return f"_No latency data for transport={transport}, size={_fmt_size(msg_size)}_\n"

        rows: list[str] = []
        header = "| Method | min µs | p50 µs | p95 µs | p99 µs | max µs |"
        sep    = "|--------|-------:|-------:|-------:|-------:|-------:|"
        rows.append(f"### Latency — {transport} — {_fmt_size(msg_size)}\n")
        rows.append(header)
        rows.append(sep)
        for comp in _competitors_ordered(data):
            s = data[comp]
            rows.append(
                f"| {comp} | {s.min:.1f} | {s.p50:.1f} | {s.p95:.1f} | {s.p99:.1f} | {s.max:.1f} |"
            )
        return "\n".join(rows) + "\n"

    # ---- Bandwidth ---------------------------------------------------------

    @staticmethod
    def bandwidth_table(
        results: Results,
        transport: str,
        msg_size: int,
    ) -> str:
        """Build a Markdown table for bandwidth (MB/s)."""
        data: dict[str, Stats] = results.get("bandwidth", {}).get(transport, {}).get(msg_size, {})
        if not data:
            return f"_No bandwidth data for transport={transport}, size={_fmt_size(msg_size)}_\n"

        rows: list[str] = []
        header = "| Method | min MB/s | p50 MB/s | p95 MB/s | p99 MB/s | max MB/s |"
        sep    = "|--------|----------:|---------:|---------:|---------:|---------:|"
        rows.append(f"### Bandwidth — {transport} — {_fmt_size(msg_size)}\n")
        rows.append(header)
        rows.append(sep)
        for comp in _competitors_ordered(data):
            s = data[comp]
            rows.append(
                f"| {comp} | {s.min:.2f} | {s.p50:.2f} | {s.p95:.2f} | {s.p99:.2f} | {s.max:.2f} |"
            )
        return "\n".join(rows) + "\n"

    # ---- Ops/sec -----------------------------------------------------------

    @staticmethod
    def ops_table(
        results: Results,
        transport: str,
        msg_size: int,
    ) -> str:
        """Build a Markdown table for ops/sec (mean only)."""
        data: dict[str, Stats] = results.get("ops", {}).get(transport, {}).get(msg_size, {})
        if not data:
            return f"_No ops data for transport={transport}, size={_fmt_size(msg_size)}_\n"

        rows: list[str] = []
        header = "| Method | ops/sec |"
        sep    = "|--------|--------:|"
        rows.append(f"### Ops/sec — {transport} — {_fmt_size(msg_size)}\n")
        rows.append(header)
        rows.append(sep)
        for comp in _competitors_ordered(data):
            s = data[comp]
            rows.append(f"| {comp} | {s.mean:,.0f} |")
        return "\n".join(rows) + "\n"

    # ---- Full report -------------------------------------------------------

    @classmethod
    def full_report(cls, results: Results, output_dir: Path) -> None:
        """Write one .md file per metric per transport."""
        output_dir.mkdir(parents=True, exist_ok=True)
        transports = set()
        msg_sizes: set[int] = set()
        for metric in results.values():
            for transport, sizes in metric.items():
                transports.add(transport)
                msg_sizes.update(sizes.keys())

        for transport in sorted(transports):
            for metric, method in [
                ("latency",   cls.latency_table),
                ("bandwidth", cls.bandwidth_table),
                ("ops",       cls.ops_table),
            ]:
                parts: list[str] = [f"# {metric.capitalize()} — {transport}\n"]
                for ms in sorted(msg_sizes):
                    parts.append(method(results, transport, ms))  # type: ignore[call-arg]
                    parts.append("")
                path = output_dir / f"{metric}_{transport}.md"
                path.write_text("\n".join(parts), encoding="utf-8")
                print(f"  wrote {path}")


# ---------------------------------------------------------------------------
# Plot reporter
# ---------------------------------------------------------------------------

_PALETTE = sns.color_palette("tab10")


class PlotReporter:
    """Generate matplotlib figures with seaborn styling."""

    _FIGSIZE = (14, 6)

    @staticmethod
    def _competitor_colors(competitors: list[str]) -> dict[str, Any]:
        return {c: _PALETTE[i % len(_PALETTE)] for i, c in enumerate(competitors)}

    # ---- Bar plot (latency / bandwidth) ------------------------------------

    @classmethod
    def _bar_figure(
        cls,
        results: Results,
        metric: str,
        transport: str,
        msg_sizes: list[int],
        ylabel: str,
        title: str,
    ) -> plt.Figure:
        """Draw grouped bars with p50 body, semi-transparent p50→p95 overlay, and min/p99 whiskers."""
        sns.set_theme(style="whitegrid")
        fig, ax = plt.subplots(figsize=cls._FIGSIZE)

        metric_data = results.get(metric, {}).get(transport, {})
        if not metric_data:
            ax.set_title(f"No data — {title}")
            return fig

        # Gather all competitors across all sizes
        all_comps: list[str] = []
        for ms in msg_sizes:
            if ms in metric_data:
                all_comps = _competitors_ordered(metric_data[ms])
                break

        n_comps = len(all_comps)
        n_sizes = len(msg_sizes)
        if n_comps == 0:
            return fig

        colors = cls._competitor_colors(all_comps)

        group_width = 0.8
        bar_width = group_width / n_comps
        x_centers = np.arange(n_sizes)

        legend_handles: list[mpatches.Patch] = []
        for ci, comp in enumerate(all_comps):
            offsets = (ci - n_comps / 2 + 0.5) * bar_width
            xs = x_centers + offsets
            color = colors[comp]

            p50s, p95s, p99s, mins_ = [], [], [], []
            for ms in msg_sizes:
                s: Stats | None = metric_data.get(ms, {}).get(comp)
                if s is None:
                    p50s.append(math.nan); p95s.append(math.nan)
                    p99s.append(math.nan); mins_.append(math.nan)
                else:
                    p50s.append(s.p50); p95s.append(s.p95)
                    p99s.append(s.p99); mins_.append(s.min)

            p50s = np.array(p50s)
            p95s = np.array(p95s)
            p99s = np.array(p99s)
            mins_ = np.array(mins_)

            # Base bar up to p50
            ax.bar(xs, p50s, width=bar_width * 0.9, color=color, alpha=0.85, label=comp)
            # Semi-transparent overlay p50 → p95
            overlay_h = np.where(np.isnan(p95s - p50s), 0, p95s - p50s)
            ax.bar(xs, overlay_h, width=bar_width * 0.9, bottom=p50s, color=color, alpha=0.30)
            # Whiskers: min and p99
            yerr_low = np.where(np.isnan(p50s - mins_), 0, p50s - mins_)
            yerr_high = np.where(np.isnan(p99s - p50s), 0, p99s - p50s)
            ax.errorbar(
                xs, p50s,
                yerr=[yerr_low, yerr_high],
                fmt="none", color=color, capsize=4, linewidth=1.2,
            )

            legend_handles.append(
                mpatches.Patch(color=color, label=comp)
            )

        ax.set_xticks(x_centers)
        ax.set_xticklabels([_fmt_size(ms) for ms in msg_sizes])
        ax.set_xlabel("Message size")
        ax.set_ylabel(ylabel)
        ax.set_title(f"{title} — {transport}")
        ax.legend(handles=legend_handles, loc="upper left", fontsize=8)

        # Add annotation explaining bar encoding
        ax.annotate(
            "bar=p50 │ shading=p95 │ whiskers=min/p99",
            xy=(0.99, 0.02), xycoords="axes fraction",
            ha="right", va="bottom", fontsize=7, color="gray",
        )
        fig.tight_layout()
        return fig

    @classmethod
    def latency_barplot(
        cls, results: Results, transport: str, msg_sizes: list[int], output_dir: Path
    ) -> None:
        fig = cls._bar_figure(
            results, "latency", transport, msg_sizes,
            ylabel="Latency (µs)", title="Latency",
        )
        path = output_dir / f"latency_{transport}.png"
        fig.savefig(path, dpi=150)
        plt.close(fig)
        print(f"  wrote {path}")

    @classmethod
    def bandwidth_barplot(
        cls, results: Results, transport: str, msg_sizes: list[int], output_dir: Path
    ) -> None:
        fig = cls._bar_figure(
            results, "bandwidth", transport, msg_sizes,
            ylabel="Bandwidth (MB/s)", title="Bandwidth",
        )
        path = output_dir / f"bandwidth_{transport}.png"
        fig.savefig(path, dpi=150)
        plt.close(fig)
        print(f"  wrote {path}")

    # ---- Line plot (ops/sec) -----------------------------------------------

    @classmethod
    def ops_lineplot(
        cls, results: Results, transport: str, msg_sizes: list[int], output_dir: Path
    ) -> None:
        """One line per competitor; X = msg_size (log scale), Y = mean ops/sec."""
        sns.set_theme(style="whitegrid")
        fig, ax = plt.subplots(figsize=cls._FIGSIZE)

        metric_data = results.get("ops", {}).get(transport, {})
        if not metric_data:
            ax.set_title("No data — Ops/sec")
            path = output_dir / f"ops_{transport}.png"
            fig.savefig(path, dpi=150)
            plt.close(fig)
            print(f"  wrote {path}")
            return

        all_comps: list[str] = []
        for ms in msg_sizes:
            if ms in metric_data:
                all_comps = _competitors_ordered(metric_data[ms])
                break

        colors = cls._competitor_colors(all_comps)

        for comp in all_comps:
            xs, ys = [], []
            for ms in msg_sizes:
                s: Stats | None = metric_data.get(ms, {}).get(comp)
                if s is not None and not math.isnan(s.mean):
                    xs.append(ms)
                    ys.append(s.mean)
            if xs:
                ax.plot(xs, ys, marker="o", label=comp, color=colors[comp])

        ax.set_xscale("log")
        ax.set_xticks(msg_sizes)
        ax.set_xticklabels([_fmt_size(ms) for ms in msg_sizes])
        ax.set_xlabel("Message size")
        ax.set_ylabel("ops/sec (mean)")
        ax.set_title(f"Operations per second — {transport}")
        ax.legend(loc="upper right", fontsize=8)
        fig.tight_layout()

        path = output_dir / f"ops_{transport}.png"
        fig.savefig(path, dpi=150)
        plt.close(fig)
        print(f"  wrote {path}")

    # ---- Full report -------------------------------------------------------

    @classmethod
    def full_report(cls, results: Results, output_dir: Path, msg_sizes: list[int]) -> None:
        output_dir.mkdir(parents=True, exist_ok=True)
        transports: set[str] = set()
        for metric in results.values():
            transports.update(metric.keys())

        for transport in sorted(transports):
            cls.latency_barplot(results, transport, msg_sizes, output_dir)
            cls.bandwidth_barplot(results, transport, msg_sizes, output_dir)
            cls.ops_lineplot(results, transport, msg_sizes, output_dir)
