#!/usr/bin/env python3
import argparse
import itertools
import json
import statistics
from pathlib import Path

PUBLIC_FIELDS = (
    "model",
    "task",
    "label",
    "mode",
    "repeat",
    "wall_s",
    "process_s",
    "usage",
    "total_tokens",
    "tool_calls",
    "fff_calls",
    "success",
    "completed",
    "timed_out",
    "exit_code",
    "targets_found",
    "targets_total",
)


def main():
    parser = argparse.ArgumentParser(
        description="Render measured agent search comparisons."
    )
    parser.add_argument("results", type=Path, nargs="+")
    parser.add_argument("--labels", nargs="+", required=True)
    parser.add_argument("--chart", type=Path, default=Path("chart.png"))
    parser.add_argument("--presentation", type=Path)
    parser.add_argument(
        "--summary", type=Path, default=Path("scripts/search-benchmark-results.json")
    )
    args = parser.parse_args()
    if len(args.labels) != len(args.results) or len(set(args.labels)) != len(
        args.labels
    ):
        parser.error("Supply one unique public label for each result directory")
    try:
        datasets = [
            load_results(path, label) for path, label in zip(args.results, args.labels)
        ]
    except ValueError as error:
        parser.error(str(error))
    summary = {"datasets": datasets}
    if args.presentation:
        presentation = json.loads(args.presentation.read_text())
        if not presentation.get("note"):
            parser.error("Presentation labels require a source note")
        summary["presentation"] = presentation
    args.summary.parent.mkdir(parents=True, exist_ok=True)
    args.summary.write_text(json.dumps(summary, indent=2) + "\n")
    render(summary, args.chart)
    for dataset in datasets:
        print(dataset["label"])
        for model, arms in dataset["models"].items():
            print(f"  {model_label(summary, model)}")
            for mode in ["native", "fff"]:
                a = arms[mode]
                tokens = (
                    f"{a['total_tokens']:,.0f}"
                    if a["tokens_complete"]
                    else "unavailable"
                )
                print(
                    f"    {mode:6} {a['wall_s']:.1f}s  {tokens} tokens  correct {a['successes']}/{a['trials']}"
                )
            print(f"    wall reduction: {arms['wall_reduction_pct']:+.1f}%")
            delta = arms["token_reduction_pct"]
            print(
                f"    token reduction: {delta:+.1f}%"
                if delta is not None
                else "    token usage incomplete"
            )


def load_results(path, label):
    manifest = json.loads((path / "manifest.json").read_text())
    rows = [json.loads(p.read_text()) for p in sorted(path.glob("*/result.json"))]
    if manifest.get("bridge_concurrency") != "multiplexed" and any(
        r.get("mcp_parallel_requests", 0) for r in rows
    ):
        raise ValueError(
            f"{label}: concurrent requests were serialized by the MCP bridge"
        )
    expected = set(
        itertools.product(
            manifest["models"],
            [t["id"] for t in manifest["tasks"]],
            range(1, manifest["repeats"] + 1),
            ["native", "fff"],
        )
    )
    actual = [(r["model"], r["task"], r["repeat"], r["mode"]) for r in rows]
    if len(actual) != len(set(actual)) or set(actual) != expected:
        raise ValueError(
            f"{label}: expected {len(expected)} unique paired trials, found {len(actual)}"
        )
    if not manifest["resident_mcp"]:
        raise ValueError(
            f"{label}: startup-inclusive results need a separate comparison"
        )
    models = {}
    for model in manifest["models"]:
        arms = {}
        for mode in ["native", "fff"]:
            arm = [r for r in rows if r["model"] == model and r["mode"] == mode]
            arms[mode] = {
                "trials": len(arm),
                "successes": sum(r["success"] for r in arm),
                "wall_s": statistics.mean(r["wall_s"] for r in arm),
                "total_tokens": statistics.mean(r["total_tokens"] for r in arm),
                "tokens_complete": all(
                    r["completed"]
                    and "input_tokens" in r["usage"]
                    and "output_tokens" in r["usage"]
                    for r in arm
                ),
                "tool_calls": statistics.mean(r["tool_calls"] for r in arm),
            }
        arms["wall_reduction_pct"] = reduction(arms, "wall_s")
        arms["token_reduction_pct"] = (
            reduction(arms, "total_tokens")
            if all(arms[m]["tokens_complete"] for m in ["native", "fff"])
            else None
        )
        models[model] = arms
    return {
        "label": label,
        "method": manifest,
        "models": models,
        "trials": [{key: row[key] for key in PUBLIC_FIELDS} for row in rows],
    }


def render(summary, destination):
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    import numpy as np
    from matplotlib.ticker import FuncFormatter

    background, foreground, muted = "#11161D", "#EDF0F5", "#A5AFBE"
    colors = {"native": "#8D9BAB", "fff": "#F87216"}
    plt.rcParams.update(
        {
            "font.family": "DejaVu Sans",
            "font.size": 11,
            "text.color": foreground,
            "axes.labelcolor": muted,
            "xtick.color": muted,
            "ytick.color": muted,
            "axes.facecolor": background,
            "figure.facecolor": background,
        }
    )
    comparisons = [
        (d, model, arms)
        for d in summary["datasets"]
        for model, arms in d["models"].items()
    ]
    fig, axes = plt.subplots(1, 2, figsize=(15, max(7, 3.6 + 0.9 * len(comparisons))))
    fig.suptitle(
        "FFF MCP vs normal shell search",
        x=0.04,
        y=0.98,
        ha="left",
        fontsize=25,
        fontweight="bold",
    )
    fig.text(
        0.04,
        0.89,
        "Concept discovery · end-to-end agent time · warmed indexes · lower is better",
        color=muted,
        fontsize=12,
    )
    labels = [
        f"{d['label']}\n{model_label(summary, model)}"
        for d, model, _ in comparisons
    ]
    for ax, metric, title in zip(
        axes,
        ["wall_s", "total_tokens"],
        ["Mean wall time / task", "Mean tokens / task"],
    ):
        ax.set_title(title, loc="left", pad=18, fontsize=15, fontweight="bold")
        if metric == "total_tokens" and any(
            not arms[mode]["tokens_complete"]
            for _, _, arms in comparisons
            for mode in colors
        ):
            ax.text(
                0.5,
                0.5,
                "Token totals unavailable for incomplete trials",
                ha="center",
                transform=ax.transAxes,
                color=muted,
            )
            ax.set_xticks([])
            ax.set_yticks([])
            continue
        y = np.arange(len(comparisons))
        for mode, offset in [("native", -0.18), ("fff", 0.18)]:
            values = [arms[mode][metric] for _, _, arms in comparisons]
            bars = ax.barh(
                y + offset,
                values,
                height=0.32,
                color=colors[mode],
                label="Normal shell" if mode == "native" else "FFF MCP",
            )
            values_text = [
                f"{v:.1f}s" if metric == "wall_s" else f"{v / 1000:.1f}k"
                for v in values
            ]
            ax.bar_label(
                bars, labels=values_text, padding=5, color=foreground, fontsize=11
            )
        ax.set_yticks(y, labels if metric == "wall_s" else ["" for _ in labels])
        ax.invert_yaxis()
        ax.set_xlim(0, ax.get_xlim()[1] * 1.18)
        if metric == "total_tokens":
            ax.xaxis.set_major_formatter(FuncFormatter(lambda v, _: f"{v / 1000:g}k"))
        ax.grid(axis="x", color="#2B3542", linewidth=0.6)
        ax.set_axisbelow(True)
        ax.tick_params(length=0)
        for spine in ax.spines.values():
            spine.set_visible(False)
    handles, names = axes[0].get_legend_handles_labels()
    fig.legend(
        handles,
        names,
        loc="upper right",
        bbox_to_anchor=(0.98, 0.91),
        frameon=False,
        labelcolor=foreground,
        ncol=2,
        fontsize=10,
    )
    accuracy = [
        f"{d['label']} {model_label(summary, m)}: {a['native']['successes']}/{a['native']['trials']} shell, {a['fff']['successes']}/{a['fff']['trials']} FFF"
        for d, m, a in comparisons
    ]
    note = summary.get("presentation", {}).get("note")
    shift = 0.03 if note else 0
    fig.text(0.04, 0.12 + shift, "Correct answers: " + "  ·  ".join(accuracy[:2]), fontsize=9)
    if len(accuracy) > 2:
        fig.text(
            0.04, 0.09 + shift, "Correct answers: " + "  ·  ".join(accuracy[2:]), fontsize=9
        )
    methods = [
        f"{d['label']}: {len(d['method']['tasks'])} tasks × {d['method']['repeats']} repeats"
        f" · protocol {d['method']['protocol_version']} · {d['method'].get('mcp_tool_exposure', 'default')} tools"
        for d in summary["datasets"]
    ]
    fig.text(
        0.04,
        0.055 + shift,
        "  ·  ".join(methods) + " · sequential trials · failures included",
        color=muted,
        fontsize=9,
    )
    fig.text(
        0.04,
        0.025 + shift,
        "Tokens = input + output, including cached input. Fresh model context per task. MCP startup, indexing, and Git refresh excluded.",
        color=muted,
        fontsize=9,
    )
    if note:
        fig.text(0.04, 0.025, note, color=muted, fontsize=9)
    fig.subplots_adjust(left=0.16, right=0.97, top=0.78, bottom=0.25 + shift, wspace=0.13)
    destination.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(destination, dpi=160, facecolor=background)
    plt.close(fig)


def reduction(arms, metric):
    baseline = arms["native"][metric]
    return (1 - arms["fff"][metric] / baseline) * 100 if baseline else 0


def model_label(summary, model):
    labels = summary.get("presentation", {}).get("model_labels", {})
    return labels.get(
        model, model.replace("gpt-", "GPT-").replace("-sol", " Sol").replace("-astra", " Astra")
    )


if __name__ == "__main__":
    main()
