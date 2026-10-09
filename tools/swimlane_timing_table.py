#!/usr/bin/env python3
"""Task-granularity stats with AIC / AIV / MIX split.

MIX tasks (deps kernel_ids[0]>=0 and kernel_ids[1]>=0) are excluded from
AIC/AIV means. MIX instance duration = max(AIC, AIV) after rank-pairing
physical rows of the same task_id (1:2 AIV pairing when AIV≈2×AIC).

Physical duration is (end-start)/clock from aicore_tasks when present.
"""
from __future__ import annotations

import argparse
import json
import math
import re
import statistics
from collections import defaultdict
from pathlib import Path

BUCKETS: list[tuple[str, float, float | None]] = [
    ("0-5", 0.0, 5.0),
    ("5-10", 5.0, 10.0),
    ("10-15", 10.0, 15.0),
    ("15-20", 15.0, 20.0),
    ("20-30", 20.0, 30.0),
    ("30-50", 30.0, 50.0),
    ("50+", 50.0, None),
]

# When MIX has samples in 50+, HTML hist splits the tail into three bins.
BUCKETS_MIX_FINE_TAIL: list[tuple[str, float, float | None]] = [
    ("0-5", 0.0, 5.0),
    ("5-10", 5.0, 10.0),
    ("10-15", 10.0, 15.0),
    ("15-20", 15.0, 20.0),
    ("20-30", 20.0, 30.0),
    ("30-50", 30.0, 50.0),
    ("50-100", 50.0, 100.0),
    ("100-150", 100.0, 150.0),
    ("150+", 150.0, None),
]

_CORE_HINT = re.compile(r"CoreId:(\d+)")
_FUNC_HINT = re.compile(r"FuncId:(\d+)")
_KERNEL_NAME = re.compile(r"^([A-Za-z0-9_]+)")


def _percentile(sorted_vals: list[float], p: float) -> float:
    if not sorted_vals:
        return 0.0
    if len(sorted_vals) == 1:
        return sorted_vals[0]
    k = (len(sorted_vals) - 1) * (p / 100.0)
    f = math.floor(k)
    c = math.ceil(k)
    if f == c:
        return sorted_vals[int(k)]
    return sorted_vals[f] * (c - k) + sorted_vals[c] * (k - f)


def _bucket_index(us: float, buckets: list[tuple[str, float, float | None]] | None = None) -> int:
    buckets = buckets if buckets is not None else BUCKETS
    for i, (_label, lo, hi) in enumerate(buckets):
        if hi is None:
            if us >= lo:
                return i
        elif lo <= us < hi:
            return i
    return len(buckets) - 1


def hist_counts(
    vals: list[float],
    buckets: list[tuple[str, float, float | None]] | None = None,
) -> list[int]:
    buckets = buckets if buckets is not None else BUCKETS
    out = [0] * len(buckets)
    for us in vals:
        out[_bucket_index(us, buckets)] += 1
    return out


def _strip_task_suffix(name: str) -> str:
    return re.sub(r"\(r\d+t\d+\)$", "", name)


def _merge_kernel_name(name: str) -> str:
    n = re.sub(r"_ai[cv]$", "", name)
    n = re.sub(r"_spmd$", "", n)
    n = re.sub(r"_\d+$", "", n)
    return n


def _core_from_metadata(core_id: int, core_types: list[str]) -> str:
    if 0 <= core_id < len(core_types):
        t = str(core_types[core_id]).lower()
        if t.startswith("aiv"):
            return "aiv"
        if t.startswith("aic"):
            return "aic"
    return "aic" if core_id < 24 else "aiv"


def _load_name_maps(swim_dir: Path) -> tuple[dict[int, str], dict[int, str]]:
    by_func: dict[int, str] = {}
    by_task: dict[int, str] = {}
    for nm in sorted(swim_dir.glob("name_map*.json")):
        try:
            payload = json.loads(nm.read_text())
        except (OSError, json.JSONDecodeError):
            continue
        mapping = payload.get("callable_id_to_name") or payload.get("func_id_to_name") or {}
        if isinstance(mapping, dict):
            for k, v in mapping.items():
                try:
                    by_func.setdefault(int(k), str(v))
                except (TypeError, ValueError):
                    continue
    merged = swim_dir / "merged_swimlane.json"
    if not merged.exists():
        candidates = sorted(swim_dir.glob("merged_swimlane_*.json"))
        merged = candidates[-1] if candidates else merged
    if not merged.exists():
        return by_func, by_task
    data = json.loads(merged.read_text())
    evs = data if isinstance(data, list) else data.get("traceEvents", [])
    for e in evs:
        if not isinstance(e, dict) or e.get("ph") != "X" or e.get("cat") != "event":
            continue
        name = _strip_task_suffix(str(e.get("name", "")))
        stem = _KERNEL_NAME.match(name)
        display = stem.group(1) if stem else name
        args = e.get("args") or {}
        hint = str(args.get("event-hint") or "")
        fm = _FUNC_HINT.search(hint)
        if fm:
            by_func.setdefault(int(fm.group(1)), display)
        tid = args.get("taskId")
        if tid is not None:
            by_task[int(tid)] = display
    return by_func, by_task


def load_task_kinds(deps_path: Path) -> dict[int, str]:
    """task_id -> 'aic' | 'aiv' | 'mix'."""
    deps = json.loads(deps_path.read_text())
    out: dict[int, str] = {}
    for t in deps.get("tasks") or []:
        kids = t.get("kernel_ids") or []
        has_aic = len(kids) > 0 and int(kids[0]) >= 0
        has_aiv = len(kids) > 1 and int(kids[1]) >= 0
        tid = int(t["task_id"])
        if has_aic and has_aiv:
            out[tid] = "mix"
        elif has_aic:
            out[tid] = "aic"
        elif has_aiv:
            out[tid] = "aiv"
    return out


def collect_records(swim_dir: Path) -> list[dict]:
    records_path = swim_dir / "chip_swimlane_records.json"
    merged_path = swim_dir / "merged_swimlane.json"
    by_func, by_task = _load_name_maps(swim_dir)
    out: list[dict] = []

    if records_path.exists():
        raw = json.loads(records_path.read_text())
        meta = raw.get("metadata") or {}
        clock = float(meta.get("clock_freq_hz") or 50_000_000)
        core_types = [str(x).lower() for x in (meta.get("core_types") or [])]
        for rec in raw.get("aicore_tasks") or []:
            if not isinstance(rec, list) or len(rec) < 5:
                continue
            core_id, task_id, func_id, start, end = (
                int(rec[0]),
                int(rec[1]),
                int(rec[2]),
                int(rec[3]),
                int(rec[4]),
            )
            us = (end - start) * 1e6 / clock
            if us < 0:
                continue
            core = _core_from_metadata(core_id, core_types)
            kernel = by_task.get(task_id) or by_func.get(func_id) or f"func_{func_id}"
            if kernel.endswith("_aic") or kernel.endswith("_aiv"):
                kernel = re.sub(r"_ai[cv]$", "", kernel)
            out.append(
                {
                    "kernel": _merge_kernel_name(kernel),
                    "core": core,
                    "us": us,
                    "task_id": task_id,
                }
            )
        if out:
            return out

    if not merged_path.exists():
        raise FileNotFoundError(f"no swimlane JSON in {swim_dir}")

    data = json.loads(merged_path.read_text())
    evs = data if isinstance(data, list) else data.get("traceEvents", [])
    core_types: list[str] = []
    if records_path.exists():
        core_types = [
            str(x).lower()
            for x in json.loads(records_path.read_text()).get("metadata", {}).get("core_types", [])
        ]
    for e in evs:
        if not isinstance(e, dict) or e.get("ph") != "X" or e.get("cat") != "event":
            continue
        name = str(e.get("name", ""))
        if not name or name.startswith(("dispatch", "complete", "chip.")):
            continue
        args = e.get("args") or {}
        us = args.get("kernel-duration-us")
        if us is None:
            us = args.get("duration-us", e.get("dur"))
        if us is None:
            continue
        hint = str(args.get("event-hint") or "")
        cm = _CORE_HINT.search(hint)
        core_id = int(cm.group(1)) if cm else -1
        nlow = name.lower()
        if "_aiv" in nlow:
            core = "aiv"
        elif "_aic" in nlow:
            core = "aic"
        elif core_id >= 0:
            core = _core_from_metadata(core_id, core_types)
        else:
            core = "aic"
        kernel = _merge_kernel_name(_strip_task_suffix(name))
        tid = args.get("taskId")
        out.append(
            {
                "kernel": kernel,
                "core": core,
                "us": float(us),
                "task_id": int(tid) if tid is not None else -1,
            }
        )
    return out


def _mix_instances(aic: list[float], aiv: list[float]) -> list[float]:
    """Pair physical rows into MIX instance durations."""
    if not aic and not aiv:
        return []
    aic_s = sorted(aic, reverse=True)
    aiv_s = sorted(aiv, reverse=True)
    out: list[float] = []
    if len(aiv_s) >= 2 * len(aic_s) and aic_s:
        for i, a in enumerate(aic_s):
            vals = [a]
            if 2 * i < len(aiv_s):
                vals.append(aiv_s[2 * i])
            if 2 * i + 1 < len(aiv_s):
                vals.append(aiv_s[2 * i + 1])
            out.append(max(vals))
        return out
    if len(aic_s) >= 2 * len(aiv_s) and aiv_s:
        for i, v in enumerate(aiv_s):
            vals = [v]
            if 2 * i < len(aic_s):
                vals.append(aic_s[2 * i])
            if 2 * i + 1 < len(aic_s):
                vals.append(aic_s[2 * i + 1])
            out.append(max(vals))
        return out
    n = max(len(aic_s), len(aiv_s))
    for i in range(n):
        vals = []
        if i < len(aic_s):
            vals.append(aic_s[i])
        if i < len(aiv_s):
            vals.append(aiv_s[i])
        out.append(max(vals))
    return out


def _agg(vals: list[float]) -> dict:
    s = sorted(vals)
    total = sum(s)
    n = len(s)
    return {
        "count": n,
        "total_us": total,
        "mean": (total / n) if n else 0.0,
        "min": s[0] if s else 0.0,
        "max": s[-1] if s else 0.0,
        "median": statistics.median(s) if s else 0.0,
        "p90": _percentile(s, 90),
        "p99": _percentile(s, 99),
    }


def infer_task_kinds(recs: list[dict]) -> dict[int, str]:
    """When deps.json is missing, treat dual-core task_ids as MIX."""
    seen: dict[int, set[str]] = defaultdict(set)
    for r in recs:
        tid = int(r.get("task_id", -1))
        if tid < 0:
            continue
        seen[tid].add(r["core"])
    out: dict[int, str] = {}
    for tid, cores in seen.items():
        if "aic" in cores and "aiv" in cores:
            out[tid] = "mix"
        elif "aic" in cores:
            out[tid] = "aic"
        elif "aiv" in cores:
            out[tid] = "aiv"
    return out


def summarize(recs: list[dict], task_kinds: dict[int, str] | None = None) -> dict:
    task_kinds = dict(task_kinds or {})
    if not task_kinds:
        task_kinds = infer_task_kinds(recs)
    pure_aic: list[float] = []
    pure_aiv: list[float] = []
    mix_vals: list[float] = []
    by_kernel: dict[tuple[str, str], list[float]] = defaultdict(list)

    # Group physical rows by task_id for MIX pairing.
    by_tid: dict[int, dict[str, list[float]]] = defaultdict(lambda: {"aic": [], "aiv": []})
    tid_kernel: dict[int, str] = {}
    for r in recs:
        tid = int(r.get("task_id", -1))
        kind = task_kinds.get(tid)
        if kind is None:
            # Fallback: treat as pure core type when deps missing.
            kind = r["core"]
        if kind == "mix":
            by_tid[tid][r["core"]].append(r["us"])
            tid_kernel.setdefault(tid, r["kernel"])
        elif kind == "aic" and r["core"] == "aic":
            pure_aic.append(r["us"])
            by_kernel[(r["kernel"], "aic")].append(r["us"])
        elif kind == "aiv" and r["core"] == "aiv":
            pure_aiv.append(r["us"])
            by_kernel[(r["kernel"], "aiv")].append(r["us"])
        # Drop MIX physical rows from pure AIC/AIV; drop mismatched cores.

    for tid, cores in by_tid.items():
        inst = _mix_instances(cores["aic"], cores["aiv"])
        mix_vals.extend(inst)
        kname = _merge_kernel_name(tid_kernel.get(tid, "mix"))
        by_kernel[(kname, "mix")].extend(inst)

    hist = {
        "aic": hist_counts(pure_aic),
        "aiv": hist_counts(pure_aiv),
        "mix": hist_counts(mix_vals),
    }

    kernel_rows = []
    for (kernel, core), vals in sorted(
        by_kernel.items(), key=lambda kv: (-len(kv[1]), kv[0][0], kv[0][1])
    ):
        row = _agg(vals)
        row["kernel"] = kernel
        row["core"] = core
        kernel_rows.append(row)

    return {
        "aic": _agg(pure_aic),
        "aiv": _agg(pure_aiv),
        "mix": _agg(mix_vals),
        "kernels": kernel_rows,
        "hist": hist,
        "durations": {"aic": pure_aic, "aiv": pure_aiv, "mix": mix_vals},
    }


def format_markdown(summary: dict, title: str) -> str:
    lines = [f"### {title}", ""]
    lines.append("#### AIC / AIV / MIX 平均时间（MIX 不计入 AIC/AIV）")
    lines.append("")
    lines.append("| 类型 | 记录/实例数 | 总时间(us) | 平均(us) | min | max | median | P90 | P99 |")
    lines.append("|---|---:|---:|---:|---:|---:|---:|---:|---:|")
    for core in ("aic", "aiv", "mix"):
        r = summary[core]
        lines.append(
            f"| {core.upper()} | {r['count']} | {r['total_us']:.2f} | {r['mean']:.2f} | "
            f"{r['min']:.2f} | {r['max']:.2f} | {r['median']:.2f} | {r['p90']:.2f} | {r['p99']:.2f} |"
        )
    lines += ["", "#### 每种任务的执行平均时间（同类编号合并）", ""]
    lines.append("| Kernel | 类型 | 记录/实例数 | 总时间(us) | 平均(us) | min | max | median | P90 | P99 |")
    lines.append("|---|---|---:|---:|---:|---:|---:|---:|---:|---:|")
    for r in summary["kernels"]:
        lines.append(
            f"| `{r['kernel']}` | {r['core'].upper()} | {r['count']} | {r['total_us']:.2f} | "
            f"{r['mean']:.2f} | {r['min']:.2f} | {r['max']:.2f} | {r['median']:.2f} | "
            f"{r['p90']:.2f} | {r['p99']:.2f} |"
        )
    lines += ["", "#### 任务粒度分布（μs，半开区间 [lo,hi)）", ""]
    labels = [b[0] for b in BUCKETS]
    lines.append("| 类型 | " + " | ".join(labels) + " | 合计 |")
    lines.append("|---|" + "|".join(["---:"] * (len(labels) + 1)) + "|")
    for core in ("aic", "aiv", "mix"):
        counts = summary["hist"][core]
        total = sum(counts) or 1
        count_cells = " | ".join(str(c) for c in counts)
        lines.append(f"| {core.upper()} count | {count_cells} | {sum(counts)} |")
        pct_cells = " | ".join(f"{100.0 * c / total:.1f}%" for c in counts)
        lines.append(f"| {core.upper()} 占比 | {pct_cells} | 100% |")
    lines.append("")
    lines.append("直方图（条长按该类型桶内最大计数归一）：")
    lines.append("")
    lines.append("```")
    for core in ("aic", "aiv", "mix"):
        counts = summary["hist"][core]
        m = max(counts) or 1
        lines.append(core.upper())
        for lab, c in zip(labels, counts):
            bar = "█" * max(1, round(c / m * 40)) if c else ""
            lines.append(f"{lab:>6} | {bar} {c}")
        lines.append("")
    lines.append("```")
    lines.append("")
    return "\n".join(lines)


def analyze_leaf(leaf: Path) -> dict:
    deps = leaf / "deps.json"
    swim = leaf
    if not (leaf / "chip_swimlane_records.json").exists() and (leaf / "dfx_outputs").exists():
        swim = leaf / "dfx_outputs"
    kinds = load_task_kinds(deps) if deps.exists() else {}
    recs = collect_records(swim)
    return summarize(recs, kinds)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("swim_dir", type=Path)
    ap.add_argument("-o", "--output", type=Path, default=None)
    ap.add_argument("--title", default="task granularity")
    ap.add_argument("--counts-json", type=Path, default=None)
    ap.add_argument("--deps", type=Path, default=None, help="deps.json (default: sibling of swim_dir)")
    args = ap.parse_args()
    deps = args.deps
    if deps is None:
        for cand in (args.swim_dir / "deps.json", args.swim_dir.parent / "deps.json"):
            if cand.exists():
                deps = cand
                break
    kinds = load_task_kinds(deps) if deps and deps.exists() else {}
    recs = collect_records(args.swim_dir)
    summary = summarize(recs, kinds)
    md = format_markdown(summary, args.title)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(md)
        print(f"wrote {args.output}")
    else:
        print(md)
    if args.counts_json:
        payload = {
            "aic": summary["aic"],
            "aiv": summary["aiv"],
            "mix": summary["mix"],
            "hist": summary["hist"],
        }
        args.counts_json.write_text(json.dumps(payload, indent=2))
        print(f"wrote {args.counts_json}")


if __name__ == "__main__":
    main()
