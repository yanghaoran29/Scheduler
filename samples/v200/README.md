# V200 capture 叶子

四档 Scheduler capture 用的单卡 decode 样例。每个叶子有两套可跑入口：

| 路径 | 作用 |
|---|---|
| `pypto-lib-operator/run_benchmark.py` | PyPTO 构图并上板 / 采集 deps 与泳道 |
| `simpler-operator/test_qwen3_decode_layer.py` 或 `test_decode_csa.py` | 用已生成的 C++ runtime 目录 replay（不走 PyPTO codegen） |

叶子根上的 `deps.json`、`name_map.json`、`chip_swimlane_records.json` 是 **sim120 的输入**。生成步骤见仓库根 [README.md](../../README.md#怎么生成-sim120)。

钉扎版本：[VERSIONS.md](VERSIONS.md)。共享 serving 档位表：[serving_load.py](serving_load.py)（`run_benchmark.py` 用 `parents[3]` 找到本目录）。

## 叶子

| 目录 | serving case | 模型 |
|---|---|---|
| `qwen3_decode_layer/lt_batch16` | `LT_BATCH16` | Qwen3 单层 decode，batch=16，Attention SPMD=120 |
| `qwen3_decode_layer/ht_batch80` | `HT_BATCH80` | 同上，batch=80，Attention SPMD=24 |
| `deepseek_v4_flash_csa/basic_batch4_mtp1` | `BASIC_BATCH4_MTP1` | Flash CSA，B=4 S=2，无 batch-tile |
| `deepseek_v4_flash_csa/ht_batch20_mtp3` | `HT_BATCH20_MTP3` | Flash CSA，B=20 mtp=3，`batch_tile=4` |

对应的冻结图在仓库 `workloads/`，不要用本目录 JSON 默默覆盖 workload，除非你有意重生成 `cases/*.h`。

## 本机 / 上板

需要外部 PyPTO 工作区（含 `activate.sh`）、钉扎 Simpler、CANN。Scheduler 本身编不出核。

```bash
export PYPTO_WORKSPACE=/path/to/pypto-lib   # 含 activate.sh
export TASK_DEVICE=...                      # a2a3 设备

# PyPTO（叶子默认 serving case）
python samples/v200/qwen3_decode_layer/lt_batch16/pypto-lib-operator/run_benchmark.py \
  -p a2a3 -d "$TASK_DEVICE"

# Simpler replay
python samples/v200/qwen3_decode_layer/lt_batch16/simpler-operator/test_qwen3_decode_layer.py \
  -p a2a3 -d "$TASK_DEVICE"
python samples/v200/deepseek_v4_flash_csa/basic_batch4_mtp1/simpler-operator/test_decode_csa.py \
  -p a2a3 -d "$TASK_DEVICE"

# 采集泳道并打回叶子根
./samples/v200/run_leaf_onboard.sh qwen3_decode_layer/lt_batch16 lt16 "$TASK_DEVICE"
```

`run_leaf_onboard.sh` 把原始 dump 写到 `samples/v200/captures/<tag>/`（已 gitignore），再用 `pack_leaf_capture.sh` 更新该叶子的 `simpler-operator/` 与根上 JSON。
