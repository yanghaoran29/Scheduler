# Scheduler

主机侧 DAG 调度原型：painter 解依赖，dispatch 按 Cube / Vector / MIX 模拟占核，输出 **MTasks/s**。不在本仓编译或下发 NPU kernel。

静态图来自 V200 上板捕获。可跑的算子源在 `samples/v200/`；冻结 JSON 在 `workloads/`；编进二进制的 header 在 `cases/`（painter）和 `cases2/`（硬件队列）。

## 和 V200 的关系

本分支只归档 Scheduler capture 用的四档叶子。`samples/v200` 是可再跑的 PyPTO / Simpler 源；`workloads/` 是生成 header 时用的冻结快照，日常不要覆盖。

| 叶子（`samples/v200/`） | capture header / workload |
|---|---|
| `qwen3_decode_layer/lt_batch16` | `qwen3_decode_layer_lt` |
| `qwen3_decode_layer/ht_batch80` | `qwen3_decode_layer_ht` |
| `deepseek_v4_flash_csa/basic_batch4_mtp1` | `deepseek_v4_csa_lt` |
| `deepseek_v4_flash_csa/ht_batch20_mtp3` | `deepseek_v4_csa_ht` |

叶子细节与上板脚本见 [samples/v200/README.md](samples/v200/README.md)。工具链钉扎见 [samples/v200/VERSIONS.md](samples/v200/VERSIONS.md)。样例文档与结构图见 [doc/cases/README.md](doc/cases/README.md) 和 [doc/cases/capture_cases.html](doc/cases/capture_cases.html)。

5× AICore 切分方案 / 静态分析 / 详版风险（含「分块数字怎么算」）见 [doc/tiling/aicore_5x_tiling.html](doc/tiling/aicore_5x_tiling.html)。

## 目录地图

| 路径 | 作用 |
|---|---|
| `src/` | painter 接口：`scheduler.c`、`painter.c`、`dispatch.c` |
| `src2/` | 硬件队列接口 |
| `cases/` | painter 静态 DAG header |
| `cases2/` | 硬件队列 header |
| `workloads/` | 冻结的 `deps.json` / swimlane / `manifest.json` |
| `samples/v200/` | 四档 PyPTO + Simpler 样例与上板产物 |
| `tools/` | capture 生成、sim120 重排、依赖图 |
| `doc/tiling/` | 5× AICore tiling 文档合集 HTML |
| `tests/` | header / SPMD 展开单测 |

## 怎么生成 sim120

sim120 不是 120 核上板。脚本读取 **AIC=24 上板** 的每任务实测 duration，按 DAG 在 **AIC=120 / AIV=240** 上做离线 ASAP 重排，写出 Chrome Trace。

**输入**（叶子根目录，不是 `sim120_swimlane/`）：

- `deps.json`
- `name_map.json`
- `chip_swimlane_records.json`

**算法**（`tools/export_sim120_swimlane.py` 的 `simulate_120`）：

1. 用上板 records 得到每个逻辑任务的 duration。
2. 去掉部分假依赖后再调度：disjoint fold、`out_proj` cast 链、同名 sibling 串行、CSA 跨 batch-tile。
3. 依赖就绪立即下发；同核同时只跑一个任务。
4. MIX 在泳道上占 AIC+AIV 轨（默认每 MIX AIC 配 1 条 AIV）。

**前置**：当前 Python 能 `import simpler_setup.tools.swimlane_converter`（与 `samples/v200/VERSIONS.md` 里的 Simpler 一致）。缺这个模块时脚本会退出。

在仓库根执行：

```bash
python3 tools/export_sim120_swimlane.py samples/v200/qwen3_decode_layer/lt_batch16
python3 tools/export_sim120_swimlane.py samples/v200/qwen3_decode_layer/ht_batch80
python3 tools/export_sim120_swimlane.py samples/v200/deepseek_v4_flash_csa/basic_batch4_mtp1
python3 tools/export_sim120_swimlane.py samples/v200/deepseek_v4_flash_csa/ht_batch20_mtp3
```

也可用短名：`python3 tools/export_sim120_swimlane.py lt_batch16`。

**输出**写到该叶子的 `sim120_swimlane/`：

- `chip_swimlane_records.json` — 合成 records
- `merged_swimlane.json` — Chrome Trace（Perfetto / `chrome://tracing`）
- `summary.json` — span、峰值、丢掉的边
- `deps.json` / `name_map.json` — 从叶子拷贝（可能已按上面规则剪边）

可选参数：`--aic`、`--aiv`、`--mix-aiv-per-aic`、`--out`。仓内已带一份生成结果，改构图或重采上板后再跑上述命令覆盖。

## 从 capture 生成调度 header

冻结 JSON 已在 `workloads/<name>/`。检查或重写 `cases/`、`cases2/`：

```bash
python3 tools/gen_capture_cases.py --check \
  qwen3_decode_layer_lt qwen3_decode_layer_ht \
  deepseek_v4_csa_lt deepseek_v4_csa_ht

python3 tools/gen_capture_cases.py \
  qwen3_decode_layer_lt qwen3_decode_layer_ht \
  deepseek_v4_csa_lt deepseek_v4_csa_ht

./build_all.sh qwen3_decode_layer_lt
```

`duration` 单位是 ns。`make CASE=subgraph|total_graph` 只覆盖旧 Qwen3-14B 样例；新 capture 用 `build_all.sh`。

## 跑 PyPTO / Simpler 样例

本仓不内嵌编译器。上板和 Simpler replay 需要外部 PyPTO、其钉扎的 Simpler runtime、以及匹配的 CANN。入口、环境变量和脚本见 [samples/v200/README.md](samples/v200/README.md)。
