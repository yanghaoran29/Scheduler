#!/usr/bin/env python3
"""Turn one onboard swimlane directory into a sim120 swimlane.

Reads the on-board ``deps.json``, ``name_map.json`` and
``chip_swimlane_records.json``, keeps each task's measured duration, reschedules
on AIC=120 / AIV=240, and writes ``sim120_swimlane/`` (Chrome Trace + records).

    python3 tools/export_sim120_swimlane.py samples/v200/deepseek_v4_flash_csa/basic_batch4_mtp1
"""
from __future__ import annotations

import argparse
import heapq
import json
import math
import shutil
import statistics
import sys
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

from swimlane_timing_table import analyze_leaf  # noqa: E402

try:
    from simpler_setup.tools.swimlane_converter import read_perf_data
except ImportError:
    read_perf_data = None

V200 = ROOT / "samples" / "v200"
# Optional short names. A directory path does not need to be in this map.
SIM120_LEAVES: dict[str, Path] = {
    "basic_batch4_mtp1": V200 / "deepseek_v4_flash_csa/basic_batch4_mtp1",
    "ht_batch20_mtp3": V200 / "deepseek_v4_flash_csa/ht_batch20_mtp3",
    "lt_batch16": V200 / "qwen3_decode_layer/lt_batch16",
    "ht_batch80": V200 / "qwen3_decode_layer/ht_batch80",
}

WORKER_PID = 4
CLOCK_FREQ_HZ = 1_000_000  # 1 cycle = 1 µs


@dataclass
class LogicalTask:
    task_id: int
    name: str
    kind: str  # aic|aiv|mix
    block_num: int
    preds: list[int]
    duration_us: float


def _load_task_kinds(deps_path: Path) -> dict[int, str]:
    """task_id -> aic | aiv | mix, from deps kernel_ids."""
    deps = json.loads(deps_path.read_text(encoding="utf-8"))
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


def _load_perf_rows(leaf: Path) -> list[dict]:
    if read_perf_data is None:
        raise RuntimeError("read_perf_data unavailable; cannot read onboard chip_swimlane_records.json")
    path = leaf / "chip_swimlane_records.json"
    if not path.is_file() and (leaf / "dfx_outputs" / "chip_swimlane_records.json").is_file():
        path = leaf / "dfx_outputs" / "chip_swimlane_records.json"
    if not path.is_file():
        raise SystemExit(f"no chip_swimlane_records.json under {leaf}")
    return read_perf_data(path)["tasks"]


def _load_names(leaf: Path) -> dict[str, str]:
    cands = sorted(leaf.glob("name_map*.json"))
    if not cands:
        return {}
    payload = json.loads(cands[0].read_text(encoding="utf-8"))
    return {str(k): str(v) for k, v in (payload.get("callable_id_to_name") or {}).items()}


def _is_dummy_task(t: dict) -> bool:
    kids = t.get("kernel_ids") or []
    if not kids:
        return True
    return all(int(k) < 0 for k in kids)


def _preds_skip_dummy(tid: int, pred_map: dict[int, set[int]], dummy_ids: set[int]) -> set[int]:
    """Replace edges A→dummy→B with A→B."""
    out: set[int] = set()
    stack = list(pred_map.get(tid, set()))
    seen = {tid}
    while stack:
        p = stack.pop()
        if p in seen:
            continue
        seen.add(p)
        if p in dummy_ids:
            stack.extend(pred_map.get(p, set()))
        else:
            out.add(p)
    return out


def build_logical_tasks(leaf: Path) -> tuple[list[LogicalTask], dict]:
    """Onboard durations + deps, one logical task per real kernel launch."""
    deps = json.loads((leaf / "deps.json").read_text(encoding="utf-8"))
    kinds = _load_task_kinds(leaf / "deps.json")
    names = _load_names(leaf)
    rows = _load_perf_rows(leaf)
    by_tid: dict[int, list[dict]] = defaultdict(list)
    for r in rows:
        by_tid[int(r["task_id"])].append(r)

    pred_map: dict[int, set[int]] = defaultdict(set)
    for e in deps.get("edges") or []:
        try:
            p, s = int(e["pred"]), int(e["succ"])
        except (KeyError, TypeError, ValueError):
            continue
        if p != s:
            pred_map[s].add(p)

    dummy_ids = {int(t["task_id"]) for t in deps.get("tasks") or [] if _is_dummy_task(t)}
    logical: list[LogicalTask] = []
    for t in deps["tasks"]:
        tid = int(t["task_id"])
        kind = kinds.get(tid)
        if not kind:
            continue
        kids = t.get("kernel_ids") or []
        kid = next((int(k) for k in kids if int(k) >= 0), None)
        name = names.get(str(kid), f"tid_{tid}") if kid is not None else f"tid_{tid}"
        xs = by_tid.get(tid) or []
        if kind == "mix":
            durs = [r["duration_us"] for r in xs]
        elif kind == "aic":
            durs = [r["duration_us"] for r in xs if r["core_type"] == "aic"]
        else:
            durs = [r["duration_us"] for r in xs if r["core_type"] == "aiv"]
        if not durs:
            durs = [r["duration_us"] for r in xs]
        if not durs:
            continue
        raw_preds = _preds_skip_dummy(tid, pred_map, dummy_ids)
        logical.append(
            LogicalTask(
                task_id=tid,
                name=name,
                kind=kind,
                block_num=int(t.get("block_num") or 1),
                preds=sorted(p for p in raw_preds if p in kinds),
                duration_us=float(statistics.median(durs)),
            )
        )
    logical_ids = {t.task_id for t in logical}
    for t in logical:
        t.preds = [p for p in t.preds if p in logical_ids]

    summary = analyze_leaf(leaf)
    if rows:
        t0 = min(r["dispatch_time_us"] for r in rows)
        span = max(r["finish_time_us"] for r in rows) - t0
    else:
        t0, span = 0.0, 0.0
    meta = {
        "n": int(summary["aic"]["count"]) + int(summary["aiv"]["count"]) + int(summary["mix"]["count"]),
        "span_us": span,
        "t0_us": t0,
        "n_logical": len(logical),
    }
    return logical, meta


def _time_bin_peak(rows: list[dict], bin_us: float, time_key: str) -> dict:
    if not rows:
        return {"peak": 0}
    t0 = min(r["dispatch_time_us"] for r in rows)
    t1 = max(r["finish_time_us"] for r in rows)
    n_bins = max(1, int(math.ceil((t1 - t0) / bin_us)))
    counts = [0] * n_bins
    for r in rows:
        i = min(n_bins - 1, max(0, int((r[time_key] - t0) / bin_us)))
        counts[i] += 1
    return {"peak": max(counts) if counts else 0}


def dispatch_peak(rows: list[dict], bin_us: float, dedupe_task: bool = False) -> dict:
    del dedupe_task
    return _time_bin_peak(rows, bin_us, "dispatch_time_us")


def completion_peak(rows: list[dict], bin_us: float, dedupe_task: bool = False) -> dict:
    del dedupe_task
    return _time_bin_peak(rows, bin_us, "finish_time_us")


def simulate_120(
    logical: list[LogicalTask],
    aic_cores: int = 120,
    aiv_cores: int = 240,
) -> dict:
    """Deps-ready immediate dispatch; fill cores by ascending task_id."""
    by_id = {t.task_id: t for t in logical}
    remaining_preds: dict[int, set[int]] = {t.task_id: set(t.preds) for t in logical}
    successors: dict[int, list[int]] = defaultdict(list)
    for tid, preds in remaining_preds.items():
        for p in preds:
            successors[p].append(tid)

    done: set[int] = set()
    remain = {t.task_id: t.block_num for t in logical}
    in_flight: dict[int, int] = defaultdict(int)
    ready = sorted(tid for tid, ps in remaining_preds.items() if not ps)
    free_aic, free_aiv = aic_cores, aiv_cores
    finish_q: list[tuple[float, int, int, int, int]] = []
    seq = 0
    t = 0.0
    instances: list[dict] = []

    def occupy(task: LogicalTask, n: int) -> tuple[int, int]:
        nonlocal free_aic, free_aiv
        if task.kind == "aic":
            free_aic -= n
            return n, 0
        if task.kind == "aiv":
            free_aiv -= n
            return 0, n
        free_aic -= n
        free_aiv -= n
        return n, n

    def release(na: int, nv: int) -> None:
        nonlocal free_aic, free_aiv
        free_aic += na
        free_aiv += nv

    def try_complete(tid: int) -> None:
        if tid in done:
            return
        if remain.get(tid, 0) > 0 or in_flight.get(tid, 0) > 0:
            return
        done.add(tid)
        for s in successors[tid]:
            remaining_preds[s].discard(tid)
            if not remaining_preds[s] and remain.get(s, 0) > 0 and s not in ready:
                ready.append(s)

    safety = 0
    while len(done) < len(by_id):
        safety += 1
        if safety > 5_000_000:
            stuck = [tid for tid in by_id if tid not in done]
            raise RuntimeError(f"sim deadlock sample={stuck[:10]} ready={ready[:5]}")
        ready.sort()
        i = 0
        while i < len(ready):
            tid = ready[i]
            if remain.get(tid, 0) <= 0:
                ready.pop(i)
                try_complete(tid)
                continue
            task = by_id[tid]
            if task.kind == "aic":
                room = free_aic
            elif task.kind == "aiv":
                room = free_aiv
            else:
                room = min(free_aic, free_aiv)
            if room <= 0:
                i += 1
                continue
            n = min(remain[tid], room)
            na, nv = occupy(task, n)
            remain[tid] -= n
            in_flight[tid] += 1
            for _ in range(n):
                instances.append(
                    {
                        "task_id": tid,
                        "name": task.name,
                        "kind": task.kind,
                        "dispatch_time_us": t,
                        "start_time_us": t,
                        "finish_time_us": t + task.duration_us,
                        "duration_us": task.duration_us,
                        "core_type": "aic" if task.kind != "aiv" else "aiv",
                    }
                )
            seq += 1
            heapq.heappush(finish_q, (t + task.duration_us, seq, tid, na, nv))
            if remain[tid] <= 0:
                ready.pop(i)
            else:
                i += 1
        if not finish_q:
            stuck = [tid for tid in by_id if tid not in done]
            raise RuntimeError(f"sim stuck with empty finish_q sample={stuck[:10]}")
        t_fin, _, tid, na, nv = heapq.heappop(finish_q)
        t = t_fin
        release(na, nv)
        in_flight[tid] -= 1
        try_complete(tid)
        while finish_q and abs(finish_q[0][0] - t) < 1e-12:
            _, _, tid2, na2, nv2 = heapq.heappop(finish_q)
            release(na2, nv2)
            in_flight[tid2] -= 1
            try_complete(tid2)

    span = max((x["finish_time_us"] for x in instances), default=0.0)
    return {"span_us": span, "n_instances": len(instances), "instances": instances}


def _fold_family(name: str) -> str | None:
    if name.startswith("gate_fold"):
        return "gate"
    if name.startswith("up_fold"):
        return "up"
    return None


_FOLD_STAGE = {
    "gate_fold": 0,
    "gate_fold_0": 1,
    "gate_fold_1": 2,
    "gate_fold_2": 3,
    "up_fold": 0,
    "up_fold_0": 1,
    "up_fold_1": 2,
    "up_fold_2": 3,
}


def _task_names(deps: dict, id2name: dict[int, str]) -> dict[str, str]:
    names: dict[str, str] = {}
    for t in deps.get("tasks") or []:
        name = ""
        for k in t.get("kernel_ids") or []:
            if k is not None and int(k) >= 0:
                name = id2name.get(int(k), "")
                break
        names[str(t["task_id"])] = name
    return names


def _explicit_fold_chains(deps: dict, names: dict[str, str]) -> dict[str, list[str]]:
    """Tile chains from explicit stage+1 edges only.

    Tensormap also emits stage+1 edges, but silu's tensormap pred can point
    at another tile's fold_2 because the slice was recorded as the full tensor.
    """
    succ: dict[str, list[str]] = defaultdict(list)
    for e in deps.get("edges") or []:
        if e.get("source") != "explicit":
            continue
        pn, sn = names.get(str(e["pred"]), ""), names.get(str(e["succ"]), "")
        ps, ts = _FOLD_STAGE.get(pn), _FOLD_STAGE.get(sn)
        if ps is None or ts is None or _fold_family(pn) != _fold_family(sn):
            continue
        if ts == ps + 1:
            succ[str(e["pred"])].append(str(e["succ"]))

    fold_to_chain: dict[str, list[str]] = {}
    for tid, name in names.items():
        if name not in ("gate_fold", "up_fold"):
            continue
        chain = [tid]
        cur = tid
        seen = {cur}
        while True:
            nxts = [n for n in succ.get(cur, []) if n not in seen]
            if not nxts:
                break
            cur = nxts[0]
            seen.add(cur)
            chain.append(cur)
        for x in chain:
            fold_to_chain[x] = chain
    return fold_to_chain


def relax_disjoint_fold_deps(logical: list, deps: dict, id2name: dict[int, str]) -> int:
    """Drop fold→fold edges. silu waits only for its own tile's stages.

    Column ranges of different n are disjoint, so S[n] = sum_k A_k[n] does
    not order fold tasks. Explicit stage edges identify the tile; tensormap
    edges that point silu at another tile's fold_2 are dropped with them.
    """
    names = _task_names(deps, id2name)
    fold_to_chain = _explicit_fold_chains(deps, names)
    by_id = {t.task_id: t for t in logical}

    explicit_fold_pred: dict[int, set[int]] = defaultdict(set)
    for e in deps.get("edges") or []:
        if e.get("source") != "explicit":
            continue
        sn = names.get(str(e["succ"]), "")
        pn = names.get(str(e["pred"]), "")
        if not (sn == "silu" or sn.startswith("silu_")):
            continue
        if _fold_family(pn):
            explicit_fold_pred[int(e["succ"])].add(int(e["pred"]))

    removed = 0
    for t in logical:
        kept: list[int] = []
        for p in t.preds:
            pred = by_id.get(p)
            if (
                pred is not None
                and _fold_family(pred.name) is not None
                and _fold_family(pred.name) == _fold_family(t.name)
            ):
                removed += 1
                continue
            if _fold_family(pred.name if pred else "") and (
                t.name == "silu" or t.name.startswith("silu_")
            ):
                if p not in explicit_fold_pred.get(t.task_id, set()):
                    removed += 1
                    continue
            kept.append(p)
        t.preds = kept

    for t in logical:
        if not (t.name == "silu" or t.name.startswith("silu_")):
            continue
        extra: list[int] = []
        for p in list(t.preds):
            chain = fold_to_chain.get(str(p))
            if chain:
                extra.extend(int(x) for x in chain)
        if extra:
            t.preds = sorted(set(t.preds) | set(extra))
    return removed


def prune_fold_edges_in_deps(deps: dict, id2name: dict[int, str]) -> int:
    """Same relaxation as relax_disjoint_fold_deps, applied to deps.json edges."""
    names = _task_names(deps, id2name)
    fold_to_chain = _explicit_fold_chains(deps, names)

    explicit_fold_pred: dict[str, set[str]] = defaultdict(set)
    for e in deps.get("edges") or []:
        if e.get("source") != "explicit":
            continue
        sn = names.get(str(e["succ"]), "")
        pn = names.get(str(e["pred"]), "")
        if (sn == "silu" or sn.startswith("silu_")) and _fold_family(pn):
            explicit_fold_pred[str(e["succ"])].add(str(e["pred"]))

    kept_edges = []
    removed = 0
    silu_needs: dict[str, set[str]] = defaultdict(set)
    for e in deps.get("edges") or []:
        pn = names.get(str(e["pred"]), "")
        sn = names.get(str(e["succ"]), "")
        if _fold_family(pn) and _fold_family(pn) == _fold_family(sn):
            removed += 1
            continue
        if _fold_family(pn) and (sn == "silu" or sn.startswith("silu_")):
            allow = explicit_fold_pred.get(str(e["succ"]), set())
            if str(e["pred"]) not in allow:
                removed += 1
                continue
            chain = fold_to_chain.get(str(e["pred"]))
            if chain:
                silu_needs[str(e["succ"])].update(chain)
        kept_edges.append(e)

    existing = {(str(e["pred"]), str(e["succ"])) for e in kept_edges}
    for succ_id, preds in silu_needs.items():
        for pred_id in preds:
            key = (pred_id, succ_id)
            if key in existing:
                continue
            kept_edges.append(
                {
                    "pred": pred_id,
                    "succ": succ_id,
                    "arg": 0,
                    "source": "slice-disjoint",
                    "flags": ["wait"],
                    "note": "silu waits for every partial of this n-tile; stages are not ordered",
                }
            )
            existing.add(key)
    deps["edges"] = kept_edges
    return removed


def _is_out_proj(name: str) -> bool:
    return name.startswith("out_proj")


def _is_resid_cast(name: str) -> bool:
    return name.startswith("residual_rms_cast")


def _false_out_cast_pairs(deps: dict, names: dict[str, str]) -> set[tuple[str, str]]:
    """Tensormap edges with no element-level dependence.

    out_proj tiles atomic-add disjoint N-columns (or the same column with a
    commutative atomic). residual_rms_cast tiles write disjoint K-slices of
    width MLP_K_SLICE. Full-tensor overlap=covered is the alias, not the math.
    Explicit edges (cast ← the out_proj tiles that cover its slice) stay.
    """
    drop: set[tuple[str, str]] = set()
    for e in deps.get("edges") or []:
        if e.get("source") != "tensormap":
            continue
        a = names.get(str(e["pred"]), "")
        b = names.get(str(e["succ"]), "")
        if (_is_out_proj(a) and _is_out_proj(b)) or (_is_resid_cast(a) and _is_resid_cast(b)):
            drop.add((str(e["pred"]), str(e["succ"])))
        elif _is_out_proj(a) and _is_resid_cast(b):
            drop.add((str(e["pred"]), str(e["succ"])))
        elif _is_resid_cast(a) and (b.startswith("gate_proj") or b.startswith("up_proj")):
            drop.add((str(e["pred"]), str(e["succ"])))
        elif a.startswith("gate_proj") and b.startswith("gate_proj"):
            # Same per-k buffer, disjoint N columns: SPMD n<6 vs late n>=6,
            # and late tiles among themselves. Recorded as full [16, 17408].
            drop.add((str(e["pred"]), str(e["succ"])))
        elif a.startswith("up_proj") and b.startswith("up_proj"):
            drop.add((str(e["pred"]), str(e["succ"])))
    return drop


def relax_out_proj_cast_deps(logical: list, deps: dict, id2name: dict[int, str]) -> int:
    names = _task_names(deps, id2name)
    drop = _false_out_cast_pairs(deps, names)
    # A pair can carry both a tensormap edge and an explicit edge. The pred
    # set is unlabelled, so keep the pred when the explicit edge is the real
    # slice dependence (cast_2/cast_3 ← out_proj_0).
    explicit = {
        (str(e["pred"]), str(e["succ"]))
        for e in deps.get("edges") or []
        if e.get("source") == "explicit"
    }
    drop -= explicit
    removed = 0
    for t in logical:
        kept: list[int] = []
        for p in t.preds:
            if (str(p), str(t.task_id)) in drop:
                removed += 1
                continue
            kept.append(p)
        t.preds = kept
    return removed


def prune_out_proj_cast_edges(deps: dict, id2name: dict[int, str]) -> int:
    names = _task_names(deps, id2name)
    drop = _false_out_cast_pairs(deps, names)
    kept = []
    removed = 0
    for e in deps.get("edges") or []:
        if (str(e["pred"]), str(e["succ"])) in drop and e.get("source") == "tensormap":
            removed += 1
            continue
        kept.append(e)
    deps["edges"] = kept
    return removed


def narrow_csa_own_tile_deps(logical: list) -> int:
    """Drop cross-tile edges whose rows do not overlap.

    ``proj_b_mm`` is emitted as ``g * N_BATCH_TILES + tile``. Each ``proj_b_act``
    sums every group's columns of its own tile only. ``hc_post`` reads that
    tile's rows. Tensormap records the full tensor, so every consumer was
    wired to the last tile; the explicit ``proj_b_act`` list names all tiles.
    Call this after the same-name sibling pass, which otherwise fans those
    consumers back onto the whole group.
    """
    mms = sorted((t for t in logical if t.name == "proj_b_mm"), key=lambda t: t.task_id)
    acts = sorted((t for t in logical if t.name == "proj_b_act"), key=lambda t: t.task_id)
    posts = sorted((t for t in logical if t.name == "hc_post"), key=lambda t: t.task_id)
    if len(acts) < 2 or len(mms) % len(acts) != 0:
        return 0
    n_tiles = len(acts)
    mm_tile = {t.task_id: i % n_tiles for i, t in enumerate(mms)}
    act_tile = {t.task_id: i for i, t in enumerate(acts)}
    removed = 0
    for i, act in enumerate(acts):
        kept = []
        for p in act.preds:
            if p in mm_tile and mm_tile[p] != i:
                removed += 1
                continue
            kept.append(p)
        act.preds = kept
    if len(posts) == n_tiles:
        for i, post in enumerate(posts):
            kept = []
            own = acts[i].task_id
            saw_own = False
            for p in post.preds:
                if p in act_tile:
                    if act_tile[p] != i:
                        removed += 1
                        continue
                    saw_own = True
                kept.append(p)
            if not saw_own:
                kept.append(own)
            post.preds = kept
    return removed


def prune_csa_false_tile_edges(deps: dict, id2name: dict[int, str]) -> int:
    """Mirror ``narrow_csa_own_tile_deps`` into the toolkit sidecar."""
    names = _task_names(deps, id2name)
    groups: dict[str, list[str]] = defaultdict(list)
    for tid, name in names.items():
        if name:
            groups[name].append(tid)
    for tids in groups.values():
        tids.sort(key=int)
    mms = groups.get("proj_b_mm") or []
    acts = groups.get("proj_b_act") or []
    posts = groups.get("hc_post") or []
    if len(acts) < 2 or len(mms) % len(acts) != 0:
        return 0
    n_tiles = len(acts)
    mm_tile = {tid: i % n_tiles for i, tid in enumerate(mms)}
    act_tile = {tid: i for i, tid in enumerate(acts)}
    # One existing act→post edge, copied when a tile's own edge is missing.
    template = None
    for e in deps.get("edges") or []:
        if names.get(str(e["pred"])) == "proj_b_act" and names.get(str(e["succ"])) == "hc_post":
            template = e
            break
    kept = []
    removed = 0
    have_post_act: set[tuple[str, str]] = set()
    for e in deps.get("edges") or []:
        a = names.get(str(e["pred"]), "")
        b = names.get(str(e["succ"]), "")
        if a and a == b and e.get("source") == "tensormap":
            removed += 1
            continue
        if b == "proj_b_act" and a == "proj_b_mm":
            succ_i = act_tile.get(str(e["succ"]))
            pred_i = mm_tile.get(str(e["pred"]))
            if succ_i is not None and pred_i is not None and succ_i != pred_i:
                removed += 1
                continue
        if b == "hc_post" and a == "proj_b_act" and len(posts) == n_tiles:
            succ_i = posts.index(str(e["succ"])) if str(e["succ"]) in posts else None
            pred_i = act_tile.get(str(e["pred"]))
            if succ_i is not None and pred_i is not None and succ_i != pred_i:
                removed += 1
                continue
            if succ_i is not None and pred_i == succ_i:
                have_post_act.add((str(e["pred"]), str(e["succ"])))
        kept.append(e)
    if template is not None and len(posts) == n_tiles:
        for i, post in enumerate(posts):
            act = acts[i]
            if (act, post) in have_post_act:
                continue
            edge = dict(template)
            edge["pred"] = act
            edge["succ"] = post
            edge["source"] = "slice-disjoint"
            kept.append(edge)
    deps["edges"] = kept
    return removed


def parallelize_same_kernel_siblings(logical: list) -> int:
    """Run same-kernel sibling calls together on different cores.

    deps.json chains same-name tensormap edges (overlap=covered), so each
    call waits for the previous call even though they only share a kernel.
    Drop those edges and fan the external consumer in to the whole sibling
    group, so the consumer still waits for every sibling. Cross-kernel deps
    stay. Same-core exclusion is enforced later by core assignment.
    """
    by_id = {t.task_id: t for t in logical}
    parent = {t.task_id: t.task_id for t in logical}

    def find(x: int) -> int:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(a: int, b: int) -> None:
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[rb] = ra

    removed = 0
    for t in logical:
        for p in t.preds:
            pred = by_id.get(p)
            if pred is not None and pred.name == t.name:
                union(p, t.task_id)
                removed += 1

    groups: dict[int, list[int]] = defaultdict(list)
    for t in logical:
        groups[find(t.task_id)].append(t.task_id)

    for members in groups.values():
        if len(members) < 2:
            continue
        member_set = set(members)
        for t in logical:
            if t.task_id in member_set:
                continue
            if any(p in member_set for p in t.preds):
                t.preds = sorted(set(t.preds) | member_set)
        for tid in members:
            task = by_id[tid]
            task.preds = [p for p in task.preds if p not in member_set]
    return removed


def _assign_cores(
    instances: list[dict],
    *,
    aic_cores: int,
    aiv_cores: int,
    mix_aiv_per_aic: int = 1,
) -> list[dict]:
    """Expand MIX to AIC+AIV lanes; ASAP start; spread across least-busy cores."""
    aic_free = [0.0] * aic_cores
    aiv_free = [0.0] * aiv_cores
    aic_busy = [0.0] * aic_cores
    aiv_busy = [0.0] * aiv_cores
    aiv_base = aic_cores  # AIV core_ids continue after AIC

    work: list[tuple[float, str, dict]] = []
    for inst in instances:
        st = float(inst["start_time_us"])
        kind = inst["kind"]
        if kind == "mix":
            work.append((st, "aic", inst))
            for _ in range(mix_aiv_per_aic):
                work.append((st, "aiv", inst))
        elif kind == "aic":
            work.append((st, "aic", inst))
        else:
            work.append((st, "aiv", inst))
    work.sort(key=lambda x: (x[0], x[1], int(x[2]["task_id"])))

    out: list[dict] = []
    for st, lane, inst in work:
        free = aic_free if lane == "aic" else aiv_free
        busy = aic_busy if lane == "aic" else aiv_busy
        eligible = [i for i, t in enumerate(free) if t <= st + 1e-9]
        if eligible:
            # Among cores free at ASAP start, pick least accumulated busy (spread).
            cid_local = min(eligible, key=lambda i: (busy[i], i))
            start = st
        else:
            cid_local = min(range(len(free)), key=lambda i: (free[i], busy[i], i))
            start = max(st, free[cid_local])
        dur = float(inst["duration_us"])
        end = start + dur
        free[cid_local] = end
        busy[cid_local] += dur
        core_id = cid_local if lane == "aic" else aiv_base + cid_local
        out.append(
            {
                "task_id": int(inst["task_id"]),
                "name": inst["name"],
                "kind": inst["kind"],
                "core_type": lane,
                "core_id": core_id,
                "dispatch_time_us": float(inst["dispatch_time_us"]),
                "start_time_us": start,
                "end_time_us": end,
                "finish_time_us": end,
                "duration_us": dur,
                "receive_time_us": start,
                "local_setup_us": 0.0,
                "propagation_us": 0.0,
                "func_id": -1,
                "ring_id": 1,
            }
        )
    out.sort(key=lambda r: (r["start_time_us"], r["core_id"], r["task_id"]))
    return out


def _us_to_cycles(us: float) -> int:
    return max(1, int(round(us)))


def pack_cycles_no_overlap(rows: list[dict]) -> dict:
    """Per-core integer cycle pack; rewrite row times so records/chrome agree.

    clock_freq_hz=1e6 → 1 cycle = 1 µs. Returns packing stats.
    """
    by_core: dict[int, list[dict]] = defaultdict(list)
    for r in rows:
        by_core[int(r["core_id"])].append(r)

    cycle_bumps = 0
    for _cid, group in by_core.items():
        group.sort(
            key=lambda r: (
                float(r["start_time_us"]),
                int(r["task_id"]),
                r["core_type"],
            )
        )
        prev_end_c = 0
        for r in group:
            # +1 shift so t=0 still has start_cycles > 0 (matches onboard-style)
            start_c = max(1, int(round(float(r["start_time_us"]))) + 1)
            dur_c = max(1, int(round(float(r["duration_us"]))))
            if start_c < prev_end_c:
                cycle_bumps += 1
                start_c = prev_end_c
            end_c = start_c + dur_c
            prev_end_c = end_c
            # Persist cycles; µs timeline matches cycles (clock=1e6 → 1:1).
            start_us = float(start_c - 1)
            end_us = float(end_c - 1)
            r["start_cycles"] = start_c
            r["end_cycles"] = end_c
            r["duration_cycles"] = dur_c
            r["start_time_us"] = start_us
            r["end_time_us"] = end_us
            r["finish_time_us"] = end_us
            r["duration_us"] = float(dur_c)
            r["receive_time_us"] = start_us
            disp_c = max(1, int(round(float(r["dispatch_time_us"]))) + 1)
            if disp_c > start_c:
                disp_c = start_c
            r["dispatch_cycles"] = disp_c
            r["dispatch_time_us"] = float(disp_c - 1)

    rows.sort(key=lambda r: (r["start_time_us"], r["core_id"], r["task_id"]))

    overlaps = 0
    for _cid, group in by_core.items():
        group.sort(key=lambda r: (r["start_cycles"], r["end_cycles"]))
        for a, b in zip(group, group[1:]):
            if b["start_cycles"] < a["end_cycles"]:
                overlaps += 1

    return {"cycle_bumps": cycle_bumps, "same_core_overlaps": overlaps}


def write_chip_records(
    path: Path,
    rows: list[dict],
    *,
    aic_cores: int,
    aiv_cores: int,
    meta: dict,
) -> None:
    n_cores = aic_cores + aiv_cores
    core_types = ["aic"] * aic_cores + ["aiv"] * aiv_cores
    aicore_tasks = []
    sched_records = []
    for i, r in enumerate(rows):
        start_c = int(r["start_cycles"])
        end_c = int(r["end_cycles"])
        disp_c = int(r["dispatch_cycles"])
        reg = i + 1
        r["register_task_id"] = reg
        token = int(r["task_id"]) & 0xFFFFFFFFFFFFFFFF
        aicore_tasks.append([r["core_id"], token, reg, start_c, end_c, 0])
        sched_records.append([r["core_id"], reg, disp_c, end_c])

    payload = {
        "chip_swimlane_level": 4,
        "sim120": True,
        "metadata": {
            "clock_freq_hz": CLOCK_FREQ_HZ,
            "num_cores": n_cores,
            "core_types": core_types,
            "core_to_thread": list(range(n_cores)),
            "note": "Synthetic ASAP sim (120 AIC / 240 AIV); not onboard capture.",
            **meta,
        },
        "aicore_tasks": aicore_tasks,
        "scheduler_tasks": {
            "schema_version": 1,
            "producer": "aicore",
            "records": sched_records,
        },
        "scheduler_records": {"schema_version": 1, "streams": []},
        "aicpu_orchestrator_phases": [],
        "aicpu_lifecycle_records": [],
    }
    path.write_text(json.dumps(payload, ensure_ascii=False) + "\n")


def write_chrome_trace(
    path: Path,
    rows: list[dict],
    *,
    aic_cores: int,
    aiv_cores: int,
) -> None:
    """Chrome Trace Event Format aligned with onboard merged_swimlane.json."""
    events: list[dict] = [
        {"args": {"name": "Worker View"}, "cat": "__metadata", "name": "process_name", "ph": "M", "pid": WORKER_PID},
        {"args": {"sort_index": 4}, "cat": "__metadata", "name": "process_sort_index", "ph": "M", "pid": WORKER_PID},
    ]
    aiv_base = aic_cores
    for cid in range(aic_cores):
        tid = 10000 + cid * 10
        events.append(
            {
                "args": {"name": f"AIC_{cid}"},
                "cat": "__metadata",
                "name": "thread_name",
                "ph": "M",
                "pid": WORKER_PID,
                "tid": tid,
            }
        )
    for i in range(aiv_cores):
        cid = aiv_base + i
        tid = 10000 + cid * 10
        events.append(
            {
                "args": {"name": f"AIV_{cid}"},
                "cat": "__metadata",
                "name": "thread_name",
                "ph": "M",
                "pid": WORKER_PID,
                "tid": tid,
            }
        )

    for idx, r in enumerate(rows):
        tid = 10000 + int(r["core_id"]) * 10
        reg = int(r.get("register_task_id", idx + 1))
        label = f"{r['name']}(r1t{reg})"
        events.append(
            {
                "args": {
                    "event-hint": (
                        f"Task:sim120, CoreId:{r['core_id']}, "
                        f"kind:{r['kind']}, task_id:{r['task_id']}"
                    ),
                    "duration-us": r["duration_us"],
                    "kernel-duration-us": r["duration_us"],
                    "local_setup_us": 0.0,
                    "taskId": r["task_id"],
                    "sim120": True,
                },
                "cat": "event",
                "id": idx,
                "name": label,
                "ph": "X",
                "pid": WORKER_PID,
                "tid": tid,
                "ts": r["start_time_us"],
                "dur": r["duration_us"],
            }
        )

    path.write_text(json.dumps({"traceEvents": events}, ensure_ascii=False) + "\n")


def _page_n(rows: list[dict]) -> dict:
    """N_AIC / N_AIV / N_MIX with MIX counted by AIC only."""
    n_aic = n_aiv = n_mix = 0
    for r in rows:
        if r["kind"] == "mix":
            if r["core_type"] == "aic":
                n_mix += 1
        elif r["core_type"] == "aic":
            n_aic += 1
        else:
            n_aiv += 1
    return {"n_aic": n_aic, "n_aiv": n_aiv, "n_mix": n_mix, "n": n_aic + n_aiv + n_mix}


def _copy_toolkit_sidecars(leaf: Path, out: Path) -> list[str]:
    copied: list[str] = []
    for name in ("deps.json", "name_map.json"):
        src = leaf / name
        if src.is_file():
            shutil.copy2(src, out / name)
            copied.append(name)
    return copied


def resolve_onboard_dir(spec: str) -> Path:
    """Directory of an onboard swimlane, a short leaf name, or a swimlane json path."""
    if spec in SIM120_LEAVES:
        return SIM120_LEAVES[spec]
    path = Path(spec).expanduser()
    if not path.is_absolute():
        path = (Path.cwd() / path).resolve()
    if path.is_file():
        path = path.parent
    if not (path / "deps.json").is_file():
        raise SystemExit(f"{path} has no deps.json (need the onboard leaf directory)")
    if not list(path.glob("name_map*.json")):
        raise SystemExit(f"{path} has no name_map.json")
    records = path / "chip_swimlane_records.json"
    alt = path / "dfx_outputs" / "chip_swimlane_records.json"
    if not records.is_file() and not alt.is_file():
        raise SystemExit(f"{path} has no chip_swimlane_records.json")
    return path


def export_leaf(
    leaf: Path,
    *,
    aic_cores: int = 120,
    aiv_cores: int = 240,
    mix_aiv_per_aic: int = 1,
    out_dir: Path | None = None,
) -> Path:
    leaf_key = leaf.name
    logical, leaf_meta = build_logical_tasks(leaf)
    raw_deps = json.loads((leaf / "deps.json").read_text())
    raw_nm = json.loads((leaf / "name_map.json").read_text())
    raw_id2name = {int(k): v for k, v in raw_nm["callable_id_to_name"].items()}
    fold_edges_dropped = relax_disjoint_fold_deps(logical, raw_deps, raw_id2name)
    out_cast_edges_dropped = relax_out_proj_cast_deps(logical, raw_deps, raw_id2name)
    sibling_edges_dropped = parallelize_same_kernel_siblings(logical)
    csa_tile_edges_dropped = narrow_csa_own_tile_deps(logical)
    sim = simulate_120(logical, aic_cores=aic_cores, aiv_cores=aiv_cores)
    rows = _assign_cores(
        sim["instances"],
        aic_cores=aic_cores,
        aiv_cores=aiv_cores,
        mix_aiv_per_aic=mix_aiv_per_aic,
    )
    pack_stats = pack_cycles_no_overlap(rows)

    out = out_dir or (leaf / "sim120_swimlane")
    out.mkdir(parents=True, exist_ok=True)

    dpeak = dispatch_peak(rows, 5.0)
    cpeak = completion_peak(rows, 5.0)
    span = max(r["finish_time_us"] for r in rows) - min(r["dispatch_time_us"] for r in rows)
    counts = _page_n(rows)
    summary = {
        "leaf": leaf_key,
        "aic_cores": aic_cores,
        "aiv_cores": aiv_cores,
        "mix_aiv_per_aic": mix_aiv_per_aic,
        "n_logical": len(logical),
        "sim_span_us": round(sim["span_us"], 3),
        "assigned_span_us": round(span, 3),
        "sim_n_instances_pre_expand": sim["n_instances"],
        "swimlane_rows": len(rows),
        "counts_page_style": counts,
        "peak_dispatch_5us": dpeak["peak"],
        "peak_completion_5us": cpeak["peak"],
        "leaf_meas_n": leaf_meta["n"],
        "leaf_meas_span_us": round(leaf_meta["span_us"], 3),
        "cycle_bumps": pack_stats["cycle_bumps"],
        "same_core_overlaps": pack_stats["same_core_overlaps"],
        "sibling_edges_dropped": sibling_edges_dropped,
        "fold_edges_dropped": fold_edges_dropped,
        "out_cast_edges_dropped": out_cast_edges_dropped,
        "csa_tile_edges_dropped": csa_tile_edges_dropped,
    }

    write_chip_records(
        out / "chip_swimlane_records.json",
        rows,
        aic_cores=aic_cores,
        aiv_cores=aiv_cores,
        meta={"summary": summary},
    )
    write_chrome_trace(
        out / "merged_swimlane.json",
        rows,
        aic_cores=aic_cores,
        aiv_cores=aiv_cores,
    )
    sidecars = _copy_toolkit_sidecars(leaf, out)
    if fold_edges_dropped or out_cast_edges_dropped or csa_tile_edges_dropped:
        deps_path = out / "deps.json"
        deps = json.loads(deps_path.read_text())
        nm = json.loads((out / "name_map.json").read_text())
        id2name = {int(k): v for k, v in nm["callable_id_to_name"].items()}
        if fold_edges_dropped:
            prune_fold_edges_in_deps(deps, id2name)
        if out_cast_edges_dropped:
            prune_out_proj_cast_edges(deps, id2name)
        if csa_tile_edges_dropped:
            prune_csa_false_tile_edges(deps, id2name)
        deps_path.write_text(json.dumps(deps, ensure_ascii=False) + "\n")
    summary["toolkit_sidecars"] = sidecars
    (out / "summary.json").write_text(json.dumps(summary, indent=2, ensure_ascii=False) + "\n")

    if fold_edges_dropped:
        fold_note = (
            "## 去掉的 fold 依赖\n\n"
            f"删掉 fold→fold 边 {fold_edges_dropped} 条（跨 n 的 "
            "`gate_fold_2 → gate_fold` 以及 tile 内四段 RMW 链）。"
            "各 n 的列区间不交，`S[n]=Σ_k A_k[n]` 不要求这些边。"
            "同一 tile 的四段 fold 与 17 路 n 同时开始；`silu` 仍等齐本 tile 的全部分段。\n"
        )
    else:
        fold_note = ""
    if out_cast_edges_dropped:
        out_note = (
            "## 去掉的 out_proj / residual_rms_cast 依赖\n\n"
            f"删掉 tensormap 边 {out_cast_edges_dropped} 条："
            "`out_proj → out_proj` / `out_proj → out_proj_0`（列区间不交，或同一列上 atomic-add 可交换），"
            "`residual_rms_cast → _0 → _1 → _2 → _3`（各写 1024 列，互不覆盖），"
            "`gate_proj_k → gate_proj_{k+4}` / `up_proj` 同理（同一 per-k buffer 上 SPMD 与 deferred 的列不交），"
            "以及由此把整张量当成 overlap=covered 的跨消费者边。"
            "显式边保留：每个 cast 仍等覆盖自己 K 段的 out_proj。\n"
        )
    else:
        out_note = ""
    if csa_tile_edges_dropped:
        csa_note = (
            "## 去掉的跨 batch-tile 依赖\n\n"
            f"调度时去掉跨 tile 前驱 {csa_tile_edges_dropped} 条。"
            "同名 tensormap 边把整张量记成 overlap=covered，5 刀写的是互不重叠的行。"
            "`proj_b_act` 的显式 deps 原先包含全部 `proj_b_mm`；跨组（同一刀的各列）保留，跨刀删除。"
            "`hc_post` 只等自己那一刀的 `proj_b_act`。\n"
        )
    else:
        csa_note = ""

    readme = f"""# sim120 模拟泳道 — `{leaf_key}`

离线 ASAP 仿真（**非**上板）：AIC={aic_cores}，AIV={aiv_cores}（a2a3 24→×5）。
调度语义是本脚本里的 `simulate_120`：依赖就绪就立刻下发。
绑核：同名兄弟调用去掉 tensormap 串行链，放到不同核上同时开始；跨 kernel 依赖保留，且消费方等待整组兄弟结束。同核同一时刻只跑一个任务。eligible 核按累计忙碌最少分散；写出前 per-core cycle 打包保证同核无重叠。

| 项 | 值 |
|----|-----|
| 逻辑任务 | {summary['n_logical']} |
| 仿真 span | {summary['sim_span_us']} µs |
| 绑核/打包后 span | {summary['assigned_span_us']} µs |
| 页口径 N (MIX计AIC) | {counts['n']} = {counts['n_aic']}+{counts['n_aiv']}+{counts['n_mix']} |
| 泳道行数 | {summary['swimlane_rows']}（MIX 每实例 {mix_aiv_per_aic}+1 轨） |
| D_peak /5µs | {summary['peak_dispatch_5us']} |
| C_peak /5µs | {summary['peak_completion_5us']} |
| cycle_bumps / same_core_overlaps | {summary['cycle_bumps']} / {summary['same_core_overlaps']} |
| 对照实测 N / span | {summary['leaf_meas_n']} / {summary['leaf_meas_span_us']} µs |

{fold_note}
{out_note}
{csa_note}
## 打开方式

- **pypto3 toolkit**：打开本目录（需同目录 `deps.json` + `name_map.json`，任务名经 `kernel_ids` → `callable_id_to_name` 解析）
- Perfetto: https://ui.perfetto.dev/ → Open `merged_swimlane.json`
- 或 Chrome `chrome://tracing`

文件：

- [`merged_swimlane.json`](merged_swimlane.json) — Chrome Trace（120 AIC + 240 AIV 轨）
- [`chip_swimlane_records.json`](chip_swimlane_records.json) — 合成 records（可 `read_perf_data`）
- [`deps.json`](deps.json) / [`name_map.json`](name_map.json) — toolkit 侧车（自叶子目录拷贝）
- [`summary.json`](summary.json)
"""
    (out / "README.md").write_text(readme)
    return out


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument(
        "onboard",
        help="上板泳道目录（含 deps.json、name_map.json、chip_swimlane_records.json），或已知叶名",
    )
    ap.add_argument("--aic", type=int, default=120)
    ap.add_argument("--aiv", type=int, default=240)
    ap.add_argument(
        "--mix-aiv-per-aic",
        type=int,
        default=1,
        help="swimlane AIV lanes per MIX AIC instance (sim occupy is 1:1; default 1)",
    )
    ap.add_argument("--out", type=Path, default=None, help="默认写到 <onboard>/sim120_swimlane")
    args = ap.parse_args()
    leaf = resolve_onboard_dir(args.onboard)
    out = export_leaf(
        leaf,
        aic_cores=args.aic,
        aiv_cores=args.aiv,
        mix_aiv_per_aic=args.mix_aiv_per_aic,
        out_dir=args.out,
    )
    summary = json.loads((out / "summary.json").read_text())
    print(f"wrote {out}")
    print(
        f"  span={summary['assigned_span_us']}µs N={summary['counts_page_style']['n']} "
        f"rows={summary['swimlane_rows']} Dpeak5={summary['peak_dispatch_5us']} "
        f"Cpeak5={summary['peak_completion_5us']} "
        f"bumps={summary['cycle_bumps']} overlaps={summary['same_core_overlaps']} "
        f"sidecars={summary.get('toolkit_sidecars')}"
    )


if __name__ == "__main__":
    main()
