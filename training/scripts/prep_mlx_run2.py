"""Run-2 MLX split: train_run2.jsonl -> data/mlx_run2/{train,valid}.jsonl (chat format).
Holds out 30 capability demos for val (seeded); keeps ALL 85 safety + plain in train
(scarce/important). Reproducible: same seed -> same split. iters for 1 epoch printed.
"""
from __future__ import annotations

import json
import math
import random
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "data" / "train_run2.jsonl"
OUT = ROOT / "data" / "mlx_run2"
N_VALID = 30
BATCH = 2


def main():
    rows = [json.loads(l) for l in SRC.read_text().splitlines() if l.strip()]
    safety = [r for r in rows if r["suite"] == "safety"]
    cap = [r for r in rows if r["suite"] == "capability"]
    random.Random(42).shuffle(cap)
    valid = cap[:N_VALID]
    train = cap[N_VALID:] + safety
    random.Random(42).shuffle(train)
    OUT.mkdir(parents=True, exist_ok=True)
    for name, recs in (("train", train), ("valid", valid)):
        (OUT / f"{name}.jsonl").write_text(
            "\n".join(json.dumps({"messages": r["messages"]}, ensure_ascii=False) for r in recs) + "\n")
    iters = math.ceil(len(train) / BATCH)
    print(f"train={len(train)} ({len(cap)-N_VALID} cap + {len(safety)} safety) | "
          f"valid={len(valid)} | iters(1 epoch, batch {BATCH})={iters}")


if __name__ == "__main__":
    main()
