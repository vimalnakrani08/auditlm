"""Prepare MLX-LM training data from data/train.jsonl (387 clean demos).

Writes data/mlx/{train,valid}.jsonl in MLX chat format ({"messages": [...]}). A small
validation set is held out to WATCH for overfitting (val loss is the real signal). All
14 hand-curated safety demos are kept in TRAIN (scarce, precious); valid is held out from
capability only. Seeded for reproducibility.
"""

from __future__ import annotations

import json
import random
from pathlib import Path

DATA = Path(__file__).resolve().parents[1] / "data"
SRC = DATA / "train.jsonl"
OUT = DATA / "mlx"
N_VALID = 30
SEED = 42


def main():
    rows = [json.loads(l) for l in SRC.read_text(encoding="utf-8").splitlines() if l.strip()]
    safety = [r for r in rows if r["suite"] == "safety"]
    cap = [r for r in rows if r["suite"] == "capability"]
    random.Random(SEED).shuffle(cap)
    valid = cap[:N_VALID]
    train = cap[N_VALID:] + safety
    random.Random(SEED).shuffle(train)

    OUT.mkdir(parents=True, exist_ok=True)
    for name, recs in (("train", train), ("valid", valid)):
        (OUT / f"{name}.jsonl").write_text(
            "\n".join(json.dumps({"messages": r["messages"]}, ensure_ascii=False) for r in recs) + "\n",
            encoding="utf-8")
    print(f"train: {len(train)} ({len(cap) - N_VALID} capability + {len(safety)} safety)  |  "
          f"valid: {len(valid)} (capability)")
    print(f"written -> {OUT}/train.jsonl, valid.jsonl")


if __name__ == "__main__":
    main()
