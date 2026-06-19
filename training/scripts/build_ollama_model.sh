#!/bin/bash
# Build an Ollama-served model from a trained LoRA adapter, apples-to-apples with the
# baseline (same Llama-3.1 chat template + stop params, same Q4_K_M quantization).
#
#   build_ollama_model.sh <adapter_dir> <ollama_name>
#   e.g. build_ollama_model.sh adapter_1ep auditlm
#
# Path: mlx_lm.fuse (dequantize 4-bit base + fuse LoRA -> f16 HF) -> llama.cpp
# convert_hf_to_gguf (f16 GGUF) -> ollama create -q q4_K_M with the llama3.1 template.
# mlx's own GGUF export is broken for this model ("row-major" error), hence llama.cpp.
set -euo pipefail

ADAPTER="$1"; NAME="$2"
# Resolve paths relative to this script (no hardcoded absolutes): scripts/ -> training/ -> auditlm/
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")" && pwd)"
AUDITLM="$(cd "$SCRIPT_DIR/../.." && pwd)"
TRAIN="$AUDITLM/training"
BASE="mlx-community/Meta-Llama-3.1-8B-Instruct-4bit"
MLX_PY="$TRAIN/.venv/bin/python"                       # has mlx-lm
CONV_PY="$AUDITLM/rag/.venv/bin/python"                # has torch+gguf for convert
LLAMACPP="${LLAMACPP:-$HOME/llama.cpp/convert_hf_to_gguf.py}"

cd "$TRAIN"
"$MLX_PY" -m mlx_lm.fuse --model "$BASE" --adapter-path "$ADAPTER" \
    --save-path "fused/hf_$NAME" --dequantize
"$CONV_PY" "$LLAMACPP" "fused/hf_$NAME" --outfile "fused/$NAME-f16.gguf" --outtype f16

# Modelfile: the EXACT llama3.1:8b template + stop params, FROM our GGUF (apples-to-apples)
python3 - "$NAME" <<'PY'
import subprocess, sys
name = sys.argv[1]
mf = subprocess.run(["ollama","show","llama3.1:8b","--modelfile"], capture_output=True, text=True).stdout.splitlines()
ti = next(i for i,l in enumerate(mf) if l.startswith("TEMPLATE"))
pj = max(i for i,l in enumerate(mf) if l.startswith("PARAMETER"))
open(f"fused/Modelfile_{name}","w").write(f"FROM ./{name}-f16.gguf\n" + "\n".join(mf[ti:pj+1]) + "\n")
PY

cd fused
ollama create "$NAME" -q q4_K_M -f "Modelfile_$NAME"
echo "[done] ollama model '$NAME' created (Q4_K_M). Eval spec: rag:ollama:$NAME"
