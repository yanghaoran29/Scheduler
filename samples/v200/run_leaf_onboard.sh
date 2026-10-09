#!/usr/bin/env bash
# Usage: run_leaf_onboard.sh <leaf_rel> <capture_tag> <device>
# Requires PYPTO_WORKSPACE (checkout that contains activate.sh).
set -euo pipefail
V200="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LEAF_REL="${1:?leaf relpath under samples/v200, e.g. qwen3_decode_layer/lt_batch16}"
TAG="${2:?capture tag}"
DEVICE="${3:?device}"
WORKSPACE="${PYPTO_WORKSPACE:-}"
if [ -z "$WORKSPACE" ] || [ ! -f "$WORKSPACE/activate.sh" ]; then
  echo "Set PYPTO_WORKSPACE to a pypto-lib workspace that has activate.sh" >&2
  exit 2
fi
OUT="$V200/captures/$TAG"
rm -rf "$OUT"
mkdir -p "$OUT"
cd "$WORKSPACE"
# shellcheck disable=SC1091
. "$WORKSPACE/activate.sh"
export PATH="${PTOAS_ROOT:+$PTOAS_ROOT/bin:$PTOAS_ROOT:}${PATH}"
python "$V200/$LEAF_REL/pypto-lib-operator/run_benchmark.py" \
  -p a2a3 -d "$DEVICE" --skip-golden --enable-chip-swimlane 4 --enable-dep-gen \
  --dep-output-dir "$OUT"
bash "$V200/pack_leaf_capture.sh" "$V200/$LEAF_REL" "$OUT"
