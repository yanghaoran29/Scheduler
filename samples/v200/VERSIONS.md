# Toolchain pins for the four capture leaves

These samples were captured against the following revisions. Scheduler does not
vendor PyPTO, Simpler, or CANN; install them outside this repo.

| Component | Revision |
|---|---|
| pypto-lib | `092efde` on `yanghaoran29/pypto-lib-aicore-5x-tiling`, branch `aicore-5x-tiling` |
| PyPTO | `6fdd989fdf45b10011c4bd6b0291a7e6c18fd7e4` |
| Simpler | `39ce891dbb3f665e72e99b4a1387b47012fb18bd` (PyPTO runtime gitlink) |
| PTO ISA | `5a4f74cbf627d4aac2e0ce10d5e0d8b118343265` |
| PTOAS | `v0.61` |

Capture environment: a2a3, 24 AIC / 48 AIV, CANN 9.0.0. The 120-AIC sim120
output is an offline reschedule of those measured durations (see the repo-root
README), not a board measurement.

This tree only vendors the four Scheduler capture leaves:

```
samples/v200/
  qwen3_decode_layer/{lt_batch16,ht_batch80}/
  deepseek_v4_flash_csa/{basic_batch4_mtp1,ht_batch20_mtp3}/
    pypto-lib-operator/
    simpler-operator/
    chip_swimlane_records.json
    merged_swimlane.json
    deps.json
    name_map.json
    concurrency_analysis.json
    sim120_swimlane/
```

Each leaf `run_benchmark.py` defaults to that directory's serving case via
`serving_load.py`. `pypto-lib-operator/golden/` is the vendored golden harness.
The Simpler entry replays the shipped C++ runtime directory; it does not run
PyPTO code generation.

Original source copyright headers are preserved. PyPTO source and generated
artifacts are distributed subject to their upstream CANN Open Software License
Agreement Version 2.0.
