#!/usr/bin/env python3
"""Build doc/tiling/aicore_5x_tiling.html from the three 5x tiling source docs."""
from __future__ import annotations

import html
import re
from pathlib import Path

HERE = Path(__file__).resolve().parent
SCHED = HERE.parents[1]
WORKSPACE = SCHED.parent
OUT = HERE / "aicore_5x_tiling.html"

LONG_TABLE_MIN_ROWS = 5  # data rows (excluding header)


def esc(s: str) -> str:
    return html.escape(s, quote=False)


def inline(s: str) -> str:
    s = esc(s)
    s = re.sub(r"`([^`]+)`", r"<code>\1</code>", s)
    s = re.sub(r"\*\*([^*]+)\*\*", r"<strong>\1</strong>", s)
    s = re.sub(r"(?<!\*)\*([^*]+)\*(?!\*)", r"<em>\1</em>", s)
    return s


def is_table_sep(line: str) -> bool:
    return bool(re.match(r"^\|?\s*:?-{3,}", line.strip()))


def parse_table(lines: list[str], i: int) -> tuple[str, int]:
    rows = []
    while i < len(lines) and lines[i].strip().startswith("|"):
        rows.append(lines[i])
        i += 1
    if len(rows) < 2:
        return "", i
    def cells(row: str) -> list[str]:
        parts = [c.strip() for c in row.strip().strip("|").split("|")]
        return parts
    header = cells(rows[0])
    body_rows = []
    for r in rows[1:]:
        if is_table_sep(r):
            continue
        body_rows.append(cells(r))
    thead = "<thead><tr>" + "".join(f"<th>{inline(c)}</th>" for c in header) + "</tr></thead>"
    tbody = "<tbody>"
    for br in body_rows:
        # pad/truncate to header width
        while len(br) < len(header):
            br.append("")
        tbody += "<tr>" + "".join(f"<td>{inline(c)}</td>" for c in br[: len(header)]) + "</tr>"
    tbody += "</tbody>"
    table = f'<div class="table-wrap"><table class="data">{thead}{tbody}</table></div>'
    if len(body_rows) >= LONG_TABLE_MIN_ROWS:
        # try to invent a short title from first header cells
        label = "展开：长表格（" + " / ".join(header[:3]) + f"，共 {len(body_rows)} 行）"
        table = (
            f'<details class="fold-table"><summary>{esc(label)}</summary>\n'
            f"{table}\n</details>"
        )
    return table, i


def md_to_html(md: str, heading_offset: int = 0) -> str:
    lines = md.replace("\r\n", "\n").split("\n")
    out: list[str] = []
    i = 0
    in_code = False
    code_lang = ""
    code_buf: list[str] = []
    list_type = None  # 'ul' | 'ol'

    def close_list():
        nonlocal list_type
        if list_type:
            out.append(f"</{list_type}>")
            list_type = None

    while i < len(lines):
        line = lines[i]
        if line.startswith("```"):
            if not in_code:
                close_list()
                in_code = True
                code_lang = line[3:].strip()
                code_buf = []
            else:
                in_code = False
                cls = f' class="lang-{esc(code_lang)}"' if code_lang else ""
                out.append(f"<pre{cls}><code>{esc(chr(10).join(code_buf))}</code></pre>")
            i += 1
            continue
        if in_code:
            code_buf.append(line)
            i += 1
            continue
        if not line.strip():
            close_list()
            i += 1
            continue
        if line.strip().startswith("|") and i + 1 < len(lines) and is_table_sep(lines[i + 1]):
            close_list()
            tbl, i = parse_table(lines, i)
            out.append(tbl)
            continue
        m = re.match(r"^(#{1,6})\s+(.*)$", line)
        if m:
            close_list()
            level = min(6, len(m.group(1)) + heading_offset)
            text = m.group(2).strip()
            # skip duplicate top-level title if offset used for sections
            out.append(f"<h{level}>{inline(text)}</h{level}>")
            i += 1
            continue
        m = re.match(r"^(\d+)\.\s+(.*)$", line)
        if m:
            if list_type != "ol":
                close_list()
                out.append("<ol>")
                list_type = "ol"
            out.append(f"<li>{inline(m.group(2))}</li>")
            i += 1
            continue
        m = re.match(r"^[-*]\s+(.*)$", line)
        if m:
            if list_type != "ul":
                close_list()
                out.append("<ul>")
                list_type = "ul"
            out.append(f"<li>{inline(m.group(1))}</li>")
            i += 1
            continue
        if line.startswith(">"):
            close_list()
            quote = line.lstrip(">").strip()
            out.append(f'<p class="note">{inline(quote)}</p>')
            i += 1
            continue
        close_list()
        out.append(f"<p>{inline(line)}</p>")
        i += 1
    close_list()
    if in_code:
        out.append(f"<pre><code>{esc(chr(10).join(code_buf))}</code></pre>")
    return "\n".join(out)


METHOD = r'''
<section id="method">
<h2>这个数是怎么算的</h2>
<p class="note">本页是设计/分析文档的整理页，<strong>不是</strong>脚本扫仓自动生成的报表。分块数字分三类，不要混读。</p>

<div class="table-wrap">
<table class="data">
<thead><tr><th>列 / 数字类型</th><th>含义</th><th>怎么得到</th></tr></thead>
<tbody>
<tr>
<td><strong>当前逻辑分块 / 当前 BlockDim</strong></td>
<td>改参前 <code>main</code> 默认构图</td>
<td>读源码 tiling 常量<strong>手算</strong>。例如 Qwen <code>out_proj</code>：<code>N_SPLITS_OUT × K_SPLITS_OUT = 10 × 5 = 50</code>；Attention：<code>ATTN_SPMD_BLOCKS=24</code>；CSA 默认 <code>T=B×S=4×2=8</code>。</td>
</tr>
<tr>
<td><strong>目标逻辑分块 / 目标 BlockDim</strong></td>
<td>面向 120 AIC 的方案</td>
<td><strong>设计取值</strong>（改哪个 tile 常量、为何选 N 不选 K），不是上板测量值。</td>
</tr>
<tr>
<td><strong>验证占用 / span / golden</strong></td>
<td>24 AIC 设备上的实测</td>
<td>来自 <code>deps.json</code> + 四级泳道；<strong>不能</strong>反推「目标分块」表。120 AIC 目标机性能仍需另采。</td>
</tr>
</tbody>
</table>
</div>

<h3>术语</h3>
<ul>
<li><code>BlockDim</code>：一次任务调用启动的并行 Block 数；普通 <code>pl.at</code> 的 BlockDim 为 1。</li>
<li><strong>逻辑分块总数</strong>：该算子全部工作块，可跨多次 <code>pl.at</code> / <code>pl.spmd</code>，不等于同时在跑。</li>
<li><strong>切分 Batch</strong>：是 = block 索引含 Batch/Token；否 = 每 block 处理完整 Batch tile；间接 = 表达式含行组但本次不切该维。</li>
</ul>

<h3>常用公式例</h3>
<pre><code>Qwen out_proj（基线）:  N_SPLITS_OUT × K_SPLITS_OUT = 10 × 5 = 50
Qwen out_proj（目标）:  20 × 5 = 100
Qwen Attention 工作项: active_batch × NUM_KV_HEADS = 16 × 8 = 128
Qwen gate/up:          17 × 5 = 85
CSA qk_pv（目标）:     T × SPARSE_BLOCKS = 8 × 5 = 40
CSA score（目标）:     T × REDUCE_NSPLIT = 8 × 10 = 80
CSA proj_a「8×16」:    8 次调用 × 每次 BlockDim 16（≠ 一次 BlockDim 128）</code></pre>

<p class="note">长表格默认折叠，点击「展开：…」查看全表。本页不含 V200 <code>granularity/case_*.md</code> 的 AIC/AIV/MIX 耗时统计；上板样例见 <a href="../../samples/v200/README.md">samples/v200</a>，sim120 生成见仓库根 <a href="../../README.md">README</a>。</p>
</section>
'''

DOC_C_ZH = r'''# Qwen3 与 DeepSeek CSA 的 5× AICore Tiling（中文译文）

> 译自英文源：`pypto-lib-aicore-5x-tiling-private/models/qwen3_deepseek_csa_5x_aicore_tiling.md`。常量名、路径与任务名保留原文。

## 1. 目标与范围

本方案面向 AICore 数量由 24 增加到 120 的机器，保持仓库默认模型规格，不改隐藏维度、Head 数、Batch 上限或序列长度。

- Qwen3：`models/qwen3_14b/decode_fwd.py::_decode_layer`
- DeepSeek CSA：`models/deepseek_v4_flash_mtp/decode_csa.py`
- 对比基线：`main` @ `c3f0dea274f55d9648920f17c968e8564ae9fcdc`
- 后续实现基线：`aicore-5x-tiling` @ `4d6007c0fe529a1c0aa920c0e28d4be70f4af393`

实现基线已包含 Qwen Attention 的 120-block launch。后续修改必须保留 `ATTN_SPMD_BLOCKS = 120` 与默认 `BATCH_PAD = 16`；增大 Batch 会改变 benchmark 工作量，不属于纯 tiling 改动。

BlockDim 指一次任务调用启动的并行 block 数。普通 `pl.at` 的 BlockDim 为 1。逻辑分块总数描述某算子的全部工作块，这些块不一定同属一次调用，也不一定同时执行。

Batch 切分标记：

- **是**：block 索引显式包含 Batch 或 Token 维。
- **否**：每个 block 处理完整 Batch tile；并行来自 N、K、Head 等非 Batch 维。
- **间接**：表达式含行组，但本次不切该维；默认形状通常只有一个物理行组。

## 2. Qwen3 方案

### 2.1 选定的 tiling

| 任务 | 基线分块 | 目标分块 | 切分维度 | 切分 Batch | 结论 |
|---|---:|---:|---|:---:|---|
| `q_proj` | 50 | 50 | N × Split-K：`10 × 5` | 否 | 保持 |
| `k_proj` | 10 | 10 | N × Split-K：`2 × 5` | 否 | 保持 |
| `v_proj` | 10 | 10 | N × Split-K：`2 × 5` | 否 | 保持 |
| `attn_swpipe_spmd` | BlockDim 24 | BlockDim 120 | Batch × KV Head 工作项 | **是** | 调整 |
| `out_proj` | 50 | 10 个任务 × 10 = 100 | 每任务：`2 N × 5 Split-K` | 否 | 重组 |
| `gate_proj` | 85 | 17 个任务 × 5 = 85 | 每任务：`1 N × 5 Split-K` | 否 | 重组 |
| `up_proj` | 85 | 17 个任务 × 5 = 85 | 每任务：`1 N × 5 Split-K` | 否 | 重组 |
| `down_proj` | 85 | 85 | N × K 分块 | 否 | 保持 |
| `x_gamma0` | BlockDim 5 | BlockDim 5 | Hidden | 否 | 保持 |
| `silu` | 17 个普通任务 | 17 个普通任务 | Intermediate N | 否 | 保持 |
| `dcr_xgamma` | BlockDim 5 | BlockDim 5 | Hidden N | 否 | 保持 |

### 2.2 保持 Q/K/V 为 50/10/10

Q、K、V 输出工作量约 5:1:1，现有 50:10:10 分块使单 block 工作量接近。

若只把 Q 改成 100：

- Q 每 block 工作量减半，K/V 仍是更长任务；
- `100 + 10 + 10 = 120` 只在数量上填满核数，并不均衡工作量；
- QKV 汇合点仍可能等 K/V 长尾；
- 额外 Q block 可能挤占 K/V 的调度窗口。

第一目标因此保持 50/10/10。仅当目标机泳道证明 Q 独立关键且 K/V 被完全隐藏时，再试 100/10/10。三者同比例扩到 100/20/20 共 140 blocks，在 120 核上必然第二波，不作默认。

### 2.3 Attention：24 → 120

修改位置：`models/qwen3_14b/paged_attention_pypto.py`。

```text
ATTN_SPMD_BLOCKS: 24 -> 120
```

默认单个 Batch window 最多有：

```text
active_batch × NUM_KV_HEADS = 16 × 8 = 128 个逻辑工作项
```

BlockDim 120 时，前 8 个 lane 各执行两项，其余各一项。这显式切分 Batch × KV Head。active Batch 小于 15 时逻辑项不足 120，部分 AICore 会空闲。更大 Batch 应由服务负载决定，不能仅为填核。

`ATTN_SPMD_BLOCKS` 同时决定 transfer / workspace（含 `TRANSFER_ROWS`），只改 launch 维度不够。

Phase 0 现为独立 BlockDim-16 任务，每 block 用 2 条 AIV，共 32 lanes。BlockDim-120 的 Attention 依赖 Phase-0 TaskId。该生产者-消费者边替代 mixed-kernel 的 `syncall`，保证 Q/K/V GM 交接顺序，且不要求 120 个 Attention block 同时驻留。Attention 无跨 block 同步、不请求 `sync_start`，可增量调度。

### 2.4 Benchmark 夹具

默认单层 benchmark：`BATCH=16`、`MAX_SEQ=4096`、`seed=1234`。生成其它随机张量前，为每行采样一个上下文长度：

```text
[2864, 724, 1319, 3126, 3277, 3445, 665, 2042,
 3728, 2738, 280, 1258, 3227, 31, 3756, 1183]
```

seed 保证可重复；长度不同使 128 个 `(batch, KV head)` 项耗时不等。旧 BlockDim 24：lane 0–7 各 6 项，8–23 各 5 项。BlockDim 120：lane 0–7 各 2 项，8–119 各 1 项。把 Batch 从 16 改到 24 会得到 192 项，必须当作独立工作量变化。

### 2.5 Out Projection：50 → 100

增大 N 切分，保持 Split-K：

```text
N_SPLITS_OUT: 10 -> 20
K_SPLITS_OUT: 5  -> 5
OUT_TN:       512 -> 256
OUT_TK:       1024（不变）
合计:         10×5=50 -> 20×5=100
```

选择理由：

- atomic-add 扇入保持 5，而非增到 10；
- 限制 atomic 竞争与累加顺序精度风险；
- `[BATCH_PAD, OUT_TN]` 累加器从 512 列降到 256 列；
- 不切 Batch；每 block 仍处理完整 `BATCH_PAD`。

基线把 50 个逻辑块组织为 26 个普通任务 + 一个直接 BlockDim-24 SPMD。第一版 120 核曾改为一次 100-block SPMD。当前分组为十个 BlockDim-10 任务：每任务覆盖两个相邻 256 列 N tile 与全部五路 Split-K。相邻两个 Out TaskId 正好产出一个 1024 列 `residual_rms_cast` slice，保留 slice 粒度释放，且不退回「一 block 一任务」。

### 2.6 按输出 tile 聚合 Gate / Up

Gate 与 Up 各保留 `17 × 5 = 85` 逻辑块。对 17 个 intermediate N tile，各有一个 Gate `spmd(5)` 与一个 Up `spmd(5)` 计算五路 Split-K partial。Gate 与 Up 互不依赖。对应 SiLU tile 只等三个 TaskId：共享 RMS reduction、其 Gate SPMD、其 Up SPMD；不等待另外 16 个输出 tile。

Down 保留 85 逻辑块。每个 Down Split-K block 仍只依赖产生其 intermediate K slice 的那一个 SiLU，保留 17 路 Gate/Up/SiLU→Down 流水。

## 3. DeepSeek CSA 方案

DeepSeek 按指定目标执行。重复的 `score 16 → 80` 只应用一次。

默认形状：`B=4`、`S=2`、`T=B×S=8`。

| 任务 | 调用次数 | 基线 BlockDim | 目标 BlockDim | 参数调整 | 切分 Batch |
|---|---:|---:|---:|---|:---:|
| `qproj_matmul` | 1 | 64 | 128 | `QPROJ_MM_N_TILE: 512 → 256` | 否 |
| `qr_proj_matmul` | 1 | 16 | 64 | `QR_OK: 2 → 8` | 否 |
| `kv_proj_matmul` | 1 | 16 | 32 | `KV_OK: 4 → 8` | 否 |
| `idx_qr_proj_matmul` | 1 | 8 | 32 | `Q_OUT_TILE: 1024 → 256`，`MM_N_TILE: 512 → 256` | 否 |
| `idx_qr_proj_dequant` | 1 | 8 | 32 | 跟随 `Q_OUT_TILE` | 否 |
| `qr_hadamard_matmul` | 1 | 8 | 32 | `QH_MM_TILE: 64 → 16` | **是** |
| `score` | 1 | 16 | 80 | `REDUCE_NSPLIT: 2 → 10` | **是** |
| `qk_pv` | 1 | 24 | 40 | `NUM_QK_CORES: 24 → 40` | **是** |
| `proj_a_mm` | 8 | 8 | 16 | `PROJ_A_MM_N_TILE: 128 → 64` | 否 |
| `proj_b_mm` | 8 | 8 | 16 | `PROJ_B_D_TILE: 512 → 256` | 否 |
| `merge_norm` | 1 | 32 | 64 | Merge Head tile：`16 → 8` | **是** |
| 主 compressor `kv_score_proj` | 1 | 16 | 64 | `OUT_TILE: 64 → 16` | 间接 |

`proj_a_mm` / `proj_b_mm` 的「8 × 16」表示八次调用、每次 BlockDim 16，不等价于一次 BlockDim-128。

### 3.1 Batch 维细节

- `qr_hadamard_matmul`：索引为 Token × HeadTile。
- `score`：Token × cache reduction split。
- `qk_pv`：40 项 = `T × SPARSE_BLOCKS = 8 × 5`。
- `merge_norm`：`T × (H / MERGE_H_TILE)`。
- `kv_score_proj`：BlockDim 含 `BS_PAD / MM_B_TILE` 行组表达式，但默认 8 token 装入一个 16 行物理 tile；16→64 仅靠缩小输出 N tile。
- 其余 matmul 每 block 处理完整默认 Token/Batch 行；新并行来自 N 或 Split-K。

### 3.2 Merge Norm：32 → 64

不能把共享 Head tile 直接从 16 改成 8：会生成非法 `[8, 512]` FP32 cube 累加器（物理 cube 行须为 16 的倍数）；8 个 FP32 仅 32 字节，相邻 QK block 可能共享 64 字节 cache line。

因此解耦生产/消费 tile：

```text
QK_STORE_H_TILE = 16  # QK/PV 物理存储与 cache-line 粒度
MERGE_H_TILE    = 8   # 每个 merge_norm block 消费的 Head 数
```

QK/PV 继续写 16 Heads 并填满 64 字节 `mi/li` 组；两个 `merge_norm` block 分别消费每个 16-Head 组的下半与上半。得到 BlockDim 64，且不引入 8 行 cube tile 或跨 block cache-line 写。

### 3.3 主要风险

目标机有 120 个物理 AIC。某任务的 BlockDim 是其自身最大并行度，不是全图利用率：独立就绪任务可共享机器。因此需要目标机 `deps.json` 与四级泳道，才能区分「孤立的低 BlockDim」与「真实空闲区间」。

| 任务 | 120 AIC 波次 / 占用 | 风险类型 | 所需决策 |
|---|---|---|---|
| `qproj_matmul=128` | `120 + 8` | 尾波性能 | 在关键路径上对比 64 与 128 blocks |
| `qr_proj_matmul=64` | 一波，53.3% | atomic 竞争与 FP32 顺序 | 对比 Split-K 2 / 4 / 8 |
| `kv_proj_matmul=32` | 一波，26.7% | atomic 竞争 | 在其并发阶段对比 Split-K 4 / 8 |
| `idx_qr_proj_*=32` | 一波，26.7% | tile 不一致则正确性 | 强制输出 tile 不变量 |
| `qr_hadamard_matmul=32` | 一波，26.7% | 权重重复搬运 | 对比 M tile 64 / 32 / 16 |
| `score=80` | 一波，最多 66.7% | 随序列长度浪费 | 短/中/长上下文分开测 |
| `qk_pv=40` | 一波，33.3% | 工作项上限 | 除非重切 Head，否则保持 40 |
| `kv_score_proj=64` | 一波，53.3% | 窄 N 搬运与 setup | 对比 N tile 64 / 32 / 16 |

#### `qproj_matmul=128`：规则 tile + 八 block 尾波

输出宽 `64 heads × 512 = 32768`。256 列 tile 下，128 个 block 写互不重叠的 `[16, 256]` INT32 区、无 atomic，故数值安全。但在 120 AIC 上，下游要等第二波 8 个 block 完成后才能看到 TaskId 完成。旧 64-block 用 512 列 tile、每 block 约两倍工作。理想线性缩放下，一波 64 与两半波 128 孤立计算时间相近，新布局还付尾调度成本。`32768 / 120` 不是整 Cube tile，故无法均匀 120 路切分。64 vs 128 是经验关键路径选择，不能单从 BlockDim 推出。

#### `qr_proj_matmul=64` 与 `kv_proj_matmul=32`：Split-K 扇入

`qr_proj_matmul` 有八个 128 列 N 组与八路 K split；同组八个 partial atomic-add 到同一 `[16, 128]` FP32 区。Split-K 2→8 使 K slice 缩短 4 倍，同时 atomic 写流量与扇入 ×4。64 blocks 可同波驻留 120 AIC，每区八个写者可能同时活跃；累加顺序也可能改 FP32 低位。

`kv_proj_matmul` 有四个 128 列 N 组与八路 K；扇入 4→8，BlockDim 仅 16→32。它不在主 Q 路径上，可与其它就绪分支重叠，故 26.7% 孤立占用不等于整机空闲。仅当目标泳道显示并发阶段更短且 atomic 尾不更大时，才保留 Split-K 8。

#### `idx_qr_proj_*=32`：输出所有权是硬不变量

Indexer 输出宽 `64 × 128 = 8192`。`Q_OUT_TILE=256` 给出 32 个互斥任务区；`MM_N_TILE=256` 使每任务恰好一次匹配 Cube 写。若 `MM_N_TILE > Q_OUT_TILE`，相邻任务重叠。当前取相等；一般安全条件：

```text
Q_OUT_TILE % MM_N_TILE == 0
MM_N_TILE <= Q_OUT_TILE
(IDX_N_HEADS * IDX_HEAD_DIM) % Q_OUT_TILE == 0
```

Matmul 与 dequant 必须继续使用同一 `Q_OUT_TILE` 划分。这是正确性要求，不是性能旋钮。

#### `qr_hadamard_matmul=32`：更多并行行，权重重复读

行数 `T × IDX_N_HEADS = 8 × 64 = 512`。M tile 64→16 使 BlockDim 8→32，每 block 行工作量降为 1/4。每 block 仍读完整 `[128, 128]` BF16 Hadamard（32 KiB）。块级权重读合计约从 256 KiB 增到 1 MiB。cache 复用可减 HBM 流量，但去不掉重复 GM→L1 与 Cube setup。用 AIC busy、MTE2 busy 与任务墙钟对比 M=64/32/16。

#### `score=80`：有效 lane 依赖序列长度

80 blocks = `T=8` × `REDUCE_NSPLIT=10`。一个 reduction tile 为 `BLOCK_SIZE / COMPRESS_RATIO = 128 / 4 = 32` 压缩 token，故每 token 有效 lane：

```text
min(10, ceil(visible_compressed_length / 32))
```

原始可见长度 ≤128 时约 8/80 有用；约 512 时 32 有用；约 1024 时 64 有用；约 1156 以上才可能 80 全有用。零迭代 lane 不改正值但仍有调度成本。产品决策需分短/中/长上下文测量；短序列桶或宜 `REDUCE_NSPLIT=2` 或 4。

#### `qk_pv=40`：当前分解只有 40 项

默认形状恰好 `T × SPARSE_BLOCKS = 8 × 5 = 40` 独立项。只增 `NUM_QK_CORES` 会产生零迭代 lane。按 Head 再切可暴露更多并行，但当前项把一块 sparse KV 收进 L1 并在 Head 组间复用；Head 切分会重复 gather 并需改结果布局或 merge。除非测过该算法改动，否则保持 40。

#### `kv_score_proj=64`：最窄 Cube 列 tile

主 ratio-4 compressor：一个 pad 后的 16 行组投影到 1024 列。N tile 64→16 使 BlockDim 16→64。权重区仍互斥、总字节不变，但公共 `[16, 4096]` 输入被 64 个 block 消费（原 16）。无 cache 复用时输入流量约 2 MiB→8 MiB。N=16 也是最小 Cube 列 tile，固定 setup/transfer 更难摊销。这是一次 SPMD launch 而非 64 次 kernel，但每 block 仍付自己的 setup。目标机对比 N=64/32/16。

#### 目标机验收标准

1. 把 Indexer 输出 tile 关系当作编译期正确性不变量。
2. 测量 `qproj` 在 64 与 128 blocks；检查八 block 尾是否拉长图关键路径。
3. 扫 QR Split-K 2/4/8 与 KV Split-K 4/8，同时看墙钟与数值漂移。
4. 扫 Hadamard M 64/32/16 与 compressor N 64/32/16。
5. 在代表性服务序列长度上测 score，而非只测最长夹具。
6. 除非重设计 Head 分解，保持 `qk_pv=40`。
7. 只以端到端目标机墙钟接受 tiling；BlockDim 或核忙碌改善只是解释性指标，不是加速本身。

## 4. 验证要求

用仓库默认形状验证每个模型：

1. 通过 PyTorch golden。
2. 无 tile shape、L0C/L1/UB 溢出或 cache-line 共享诊断。
3. 确认 `deps.json` 未引入不必要串行依赖。
4. 用四级目标机泳道证明任务真正并发，而非只造更多短任务或新尾波。
5. 在 120 核机上测端到端关键路径时延。
6. 对每个切分 Batch 的任务，单独测真实服务 Batch，再决定是否改 Batch 配置。

## 5. 当前验证状态

- Qwen Q/K/V 与 MLP tiling 未改。
- Qwen Out Projection 为十个 BlockDim-10 SPMD。
- Qwen Gate/Up 各为 17 个 BlockDim-5 SPMD；每个 SiLU 只等匹配的 Gate/Up 与 RMS reduction。
- Qwen Phase 0 为独立 BlockDim-16 生产者；Attention 仍为 BlockDim 120，依赖 Phase-0 TaskId，无 `syncall`。
- Qwen decode 样例无 dummy；消费者直接依赖真实生产者或无效初始 scratch carry。
- Qwen decode 与独立 paged-attention 不请求 early dispatch；捕获图全部 166 个逻辑任务 `early_dispatch=false`。
- 含 BlockDim-120 Attention 的完整 Qwen 布局可在 `a2a3sim` 编译；生成 orchestration 含 10 Out、17 Gate、17 Up SPMD，无 dummy、无 `syncall`。
- 完整 Qwen 布局在 `a2a3` device 1 上通过默认单层 golden（Attention BlockDim 120）。
- DeepSeek 实现全部目标 BlockDim（含解耦的 QK 存储与 Merge Norm 消费），并在 `a2a3` device 1 通过默认 golden。
- 尚无 120 核目标机性能结果。

### 5.1 新依赖与四级泳道采集

两次样例在同一次 harness 调用中启用依赖生成与四级芯片泳道。运行时两次 device pass：第一次生成 `deps.json`，第二次采集无依赖插桩的干净 timing。

```bash
python models/qwen3_14b/decode_fwd.py -p a2a3 -d "$DEVICE" \
  --enable-chip-swimlane 4 --enable-dep-gen \
  --dep-output-dir "$QWEN_CAPTURE"

python models/deepseek_v4_flash_mtp/decode_csa.py -p a2a3 -d "$DEVICE" \
  --enable-chip-swimlane 4 --enable-dep-gen \
  --dep-output-dir "$CSA_CAPTURE"
```

可用采集设备为 24 AIC / 48 AIV（见各 `chip_swimlane_records.json`），不是 120 AIC 目标。下表证明图结构、实际重叠与数值正确性；不能预测目标机墙钟。

| 样例 | Golden | 拼接物理行 | AIC 峰值 | AIC 区间平均占用 | AIV 峰值 | AIV 区间平均占用 | 采集测试跨度 |
|---|---|---:|---:|---:|---:|---:|---:|
| Qwen 单层 | Pass | 857 / 857 | 24 / 24 | 82.5% | 48 / 48 | 19.3% | 906.76 us |
| DeepSeek CSA | Pass | 1182 / 1182 | 24 / 24 | 40.4% | 48 / 48 | 10.2% | 578.24 us |

平均占用覆盖该核类型从首到末 kernel 的区间，含有意只用另一类核的阶段。它解释采集，不是端到端加速指标。

Qwen 产物：

- `../artifacts/qwen3_decode_5x_no_early_24core_capture/dfx_outputs/deps.json`
- `../artifacts/qwen3_decode_5x_no_early_24core_capture/dfx_outputs/deps_viewer.html`
- `../artifacts/qwen3_decode_5x_no_early_24core_capture/dfx_outputs/chip_swimlane_records.json`
- `../artifacts/qwen3_decode_5x_no_early_24core_capture/dfx_outputs/merged_swimlane_20260914_093942.json`
- `../artifacts/qwen3_decode_5x_no_early_24core_capture/dfx_outputs/concurrency_analysis.json`

DeepSeek 产物：

- `../artifacts/deepseek_csa_5x_no_early_24core_capture/dfx_outputs/deps.json`
- `../artifacts/deepseek_csa_5x_no_early_24core_capture/dfx_outputs/deps_viewer.html`
- `../artifacts/deepseek_csa_5x_no_early_24core_capture/dfx_outputs/chip_swimlane_records.json`
- `../artifacts/deepseek_csa_5x_no_early_24core_capture/dfx_outputs/merged_swimlane_20260914_185837.json`
- `../artifacts/deepseek_csa_5x_no_early_24core_capture/dfx_outputs/concurrency_analysis.json`

### 5.2 Qwen 并发分析

新依赖图确认阶段级并发意图：

| 阶段 | 依赖可见的 AIC 工作 | 120 AIC 解读 |
|---|---:|---|
| Q/K/V projection | Q 50、K 10、V 10 | 独立投影分支可重叠；合计声明 70 blocks |
| Attention | 120 | 恰好一整波目标机；Phase 0 为独立 16-block AIV 任务 |
| Out Projection | 10 个独立任务 × 10 = 100 | 十任务仅共享 Attention 前驱，可暴露 100 AIC blocks（目标的 83.3%） |
| Gate + Up | 17×5 + 17×5 = 170 | 五个 cast 生产者喂给每个 Gate/Up；cast 完成后可填满 120 AIC，另 50 blocks 进后波 |
| Down | 85 个单 block 任务 | 每五路组跟随各自 SiLU，可与未完成的 Gate/Up 流水 |

24 AIC 采集上物理 AIC 并发达 24。Attention 形成最大观测逻辑包络（120 声明 blocks）。最大同时逻辑任务数为 24、声明 68 blocks（Gate/Up/Down 流水），确认关闭 early dispatch 后五 block Gate/Up 与单 block Down 仍重叠。

相对此前允许 early dispatch 的采集，测试跨度 874.76→906.76 us，AIC 区间占用 84.5%→82.5%。这是 24 AIC 上的分次运行，含噪声，不是目标机回退测量。它显示预期调度效应：消费者现等正常生产者完成，而任务数、BlockDim 与显式边不变。

本采集需去掉 Attention 过时的 `sync_start` 请求。Phase 0 已通过 TaskId 提供全局 Q/K/V 交接；Attention 无跨 block 同步。BlockDim 仍为 120。目标机上增量派发也允许其它就绪工作在 AIC 空闲时立即占用，而不要求 120 个 AIC 同时空闲。

### 5.3 DeepSeek 并发分析

本采集中 CSA 使用普通依赖调度。70 个逻辑任务全部 `early_dispatch=false`；无空 kernel 列表。RMS 延迟 dummy 换成 RMS 生产者 TaskId；去掉输出权重预取；QK/PV 按自然 token/block 序，无非空优先规划。独立 helper 包装用无效 TaskId 而非空 dummy。无 `sync_start` / `syncall`。

`kv_touch` 自拷贝保留，因其提供必需 KV-cache 依赖。Split-K atomics、显式生产者边与核内流水仍实现选定计算。共享 helper 也影响其它调用者；本采集只验证 CSA decode。

24 AIC 泳道达到满物理 AIC 占用。最大逻辑 AIC 包络 160 声明 blocks：QR projection(64)、主 compressor projection(64)、KV projection(32)。随后 Q projection(128) 与 Indexer projection(32) 重叠，亦暴露 160。输出阶段最多六个同时逻辑任务、96 blocks（跨 `proj_a_mm`/`proj_b_mm`）；八个独立 `proj_a_mm(16)` 在 Merge Norm 后静态可暴露 128 blocks。

投影分支因此可在目标机上供给超过 120 blocks。它们在真实依赖完成后争用资源，无人为延迟优先 QR。`qproj_matmul=128` 的八 block 尾仍需目标机测量。

本采集在 device 0 通过 `x_out` 与 `kv_cache`。记录跨度 578.24 us，对比更早带调度优化的 548.70 us。那是 24 AIC 上分次插桩运行，不是受控 120 AIC 性能对比。物理行 1183→1182 因去掉 prefetch kernel；去掉 dummy 降低逻辑任务数但不去掉物理计算行。

后续 attention 阶段固有并发更低，在捕获的 AIC 包络中较孤立：

| 阶段 | 声明 AIC blocks | 120 AIC 上限 | 后果 |
|---|---:|---:|---|
| `qr_hadamard_matmul` | 32 | 26.7% | 本采集中无独立 AIC 工作与之重叠 |
| `score` | 80 | 66.7% | 短序列时有效有用 blocks 可能更低 |
| `qk_pv` | 40 | 33.3% | 当前 `T × SPARSE_BLOCKS` 分解无额外项 |
| 八个 `proj_a_mm` 任务 | 128 | 100% + 8-block 尾 | Merge Norm 后八个输出组独立 |

图在早期投影扇出与分组输出投影中有足够工作填满 120 AIC。目标机上可能欠填的区域是 Hadamard、短上下文的 score、以及 QK/PV。仍强制要求新的 120 AIC 四级采集：24 AIC timing 会改变重叠形态，不能确立目标调度顺序或墙钟。
'''


CSS = """
:root{--bg:#fafafa;--txt:#222;--muted:#666;--line:#e5e5e5;--panel:#fff;--aic:#2563eb}
*{box-sizing:border-box}
body{margin:0;font-family:-apple-system,BlinkMacSystemFont,"Segoe UI","PingFang SC","Microsoft YaHei",sans-serif;
  background:var(--bg);color:var(--txt);line-height:1.65;font-size:15px}
.wrap{max-width:980px;margin:0 auto;padding:28px 20px 72px}
h1{font-size:26px;margin:0 0 8px}
h2{font-size:20px;margin:36px 0 10px;padding-bottom:6px;border-bottom:1px solid var(--line)}
h3{font-size:16px;margin:22px 0 8px}
h4{font-size:15px;margin:18px 0 6px}
.lead,.note{color:var(--muted);font-size:14px}
a{color:var(--aic);text-decoration:none}
a:hover{text-decoration:underline}
code{font-family:ui-monospace,SFMono-Regular,Consolas,monospace;font-size:0.92em;background:#f3f4f6;padding:1px 5px;border-radius:4px}
pre{background:#f3f4f6;border:1px solid var(--line);border-radius:8px;padding:12px 14px;overflow-x:auto;font-size:13px;line-height:1.45}
pre code{background:transparent;padding:0}
.table-wrap{overflow-x:auto;margin:12px 0 18px}
table.data{border-collapse:collapse;width:100%;font-size:13.5px;background:var(--panel)}
table.data th,table.data td{border:1px solid var(--line);padding:7px 10px;text-align:left;vertical-align:top}
table.data th{background:#f3f4f6;font-weight:600}
ul,ol{padding-left:1.4em}
li{margin:4px 0}
.toc{background:var(--panel);border:1px solid var(--line);border-radius:10px;padding:14px 18px;margin:18px 0 28px}
.toc h2{margin-top:0;border:none;padding:0;font-size:16px}
.toc ol{margin:8px 0 0}
.toc li{margin:3px 0}
.doc-banner{background:#eff6ff;border:1px solid #bfdbfe;border-radius:8px;padding:10px 14px;margin:12px 0 18px;font-size:14px;color:#1e3a5f}
details.fold-table{margin:12px 0 18px;border:1px solid var(--line);border-radius:8px;background:var(--panel);padding:0 12px 4px}
details.fold-table>summary{cursor:pointer;padding:10px 4px;font-weight:600;color:var(--aic);list-style-position:outside}
details.fold-table[open]>summary{border-bottom:1px solid var(--line);margin-bottom:8px}
footer.page-foot{margin-top:48px;padding-top:16px;border-top:1px solid var(--line);color:var(--muted);font-size:13px}
.sec{margin-top:8px}
"""


def strip_first_h1(md: str) -> str:
    lines = md.split("\n")
    if lines and lines[0].startswith("# "):
        return "\n".join(lines[1:]).lstrip("\n")
    return md


def main() -> None:
    doc_a = (WORKSPACE / "AICORE_5X_TILING_PLAN.md").read_text(encoding="utf-8")
    doc_b = (WORKSPACE / "AICORE_5X_STATIC_ANALYSIS_CHANGE_PLAN.md").read_text(encoding="utf-8")
    doc_c = DOC_C_ZH

    body_a = md_to_html(strip_first_h1(doc_a), heading_offset=1)
    body_b = md_to_html(strip_first_h1(doc_b), heading_offset=1)
    body_c = md_to_html(strip_first_h1(doc_c), heading_offset=1)

    html_doc = f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8"/>
<meta name="viewport" content="width=device-width, initial-scale=1.0"/>
<title>5× AICore Tiling 文档合集</title>
<style>
{CSS}
</style>
</head>
<body>
<div class="wrap">
<h1>5× AICore Tiling 文档合集</h1>
<p class="lead">面向 AICore 24→120 的 Qwen3 / DeepSeek CSA 切分方案、静态分析与详版风险说明。长表格默认折叠。</p>

<nav class="toc" id="toc">
<h2>目录</h2>
<ol>
  <li><a href="#method">这个数是怎么算的</a></li>
  <li><a href="#doc-a">文档 A：5× AICore 并行切分方案</a></li>
  <li><a href="#doc-b">文档 B：120 AICore 静态分析与修改方案</a></li>
  <li><a href="#doc-c">文档 C：Qwen3 / CSA 详版（中文译文）</a></li>
</ol>
</nav>

{METHOD}

<section class="sec" id="doc-a">
<h2>文档 A：5× AICore 并行切分方案</h2>
<div class="doc-banner">源文件：工作区根目录 <code>AICORE_5X_TILING_PLAN.md</code>（中文原文）。</div>
{body_a}
</section>

<section class="sec" id="doc-b">
<h2>文档 B：120 AICore 静态分析与修改方案</h2>
<div class="doc-banner">源文件：工作区根目录 <code>AICORE_5X_STATIC_ANALYSIS_CHANGE_PLAN.md</code>（中文原文）。</div>
{body_b}
</section>

<section class="sec" id="doc-c">
<h2>文档 C：Qwen3 / CSA 详版（中文译文）</h2>
<div class="doc-banner">译自 <code>pypto-lib-aicore-5x-tiling-private/models/qwen3_deepseek_csa_5x_aicore_tiling.md</code>。与 A/B 有重叠表，按全文要求保留。</div>
{body_c}
</section>

<footer class="page-foot">
<p>本页归档于 Scheduler <code>doc/tiling/aicore_5x_tiling.html</code>。可跑样例与上板产物见 <a href="../../samples/v200/README.md">samples/v200</a>；离线 120 核泳道生成见仓库根 <a href="../../README.md#怎么生成-sim120">README · sim120</a>。</p>
</footer>
</div>
</body>
</html>
"""
    OUT.write_text(html_doc, encoding="utf-8")
    print(f"wrote {OUT} ({OUT.stat().st_size} bytes)")


if __name__ == "__main__":
    main()
