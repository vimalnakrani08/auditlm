"""Run-2 assembly + CONTAMINATION GATE (hard stop). Merges the reworked capability set
(335 grounded-guardrail + 38 plain) with the 85 hand-curated safety demos (normalized to
the grounded-guardrail format so train matches serve), then shingle-gates the WHOLE set vs
the 166 test items. Writes data/train_run2.jsonl. Training must not start unless clean.
"""
from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "rag"))
from ground import TIERED_GUARDRAIL_POLICY  # noqa: E402

DATA = ROOT / "training" / "data"
AB = Path(os.environ.get("ASSURANCEBENCH") or ROOT.parent / "assurancebench")
OUT = DATA / "train_run2.jsonl"
SYSTEM = ("You are an assistant for US external audit (PCAOB standards) and US "
          "GAAP accounting. Answer precisely and cite standards by their identifiers "
          "(e.g. AS 2301.05, ASC 606, 17 CFR 210.2-01) where relevant.")
WORD = re.compile(r"[a-z0-9]+"); K = 8; FLAG = 0.40


def load(p): return [json.loads(l) for l in Path(p).read_text().splitlines() if l.strip()]
def sh(t): x = WORD.findall(t.lower()); return {hash(tuple(x[i:i+K])) for i in range(len(x)-K+1)}


def norm_safety(r):
    grounded = (f"{TIERED_GUARDRAIL_POLICY}\n\nRETRIEVED PASSAGES:\n{r['retrieved_context']}\n\n"
                f"QUESTION:\n{r['question']}\n\nAnswer:")
    return {"id": r["id"], "suite": "safety", "task_category": r["task_category"],
            "zone": r.get("zone"), "severity": r.get("severity"),
            "question": r["question"], "answer": r["ideal_answer"],
            "messages": [{"role": "system", "content": SYSTEM},
                         {"role": "user", "content": grounded},
                         {"role": "assistant", "content": r["ideal_answer"]}],
            "provenance": "hand-curated"}


def main():
    grounded = load(DATA / "capability_run2_grounded.jsonl")
    plain = load(DATA / "capability_run2_plain.jsonl")
    safety = [norm_safety(r) for r in load(DATA / "safety_sft_full.jsonl")]
    train = grounded + plain + safety
    from collections import Counter
    print(f"=== MERGE === grounded {len(grounded)} + plain {len(plain)} + safety {len(safety)} = {len(train)}")
    print("  capability:", sum(1 for r in train if r['suite']=='capability'),
          "| safety:", sum(1 for r in train if r['suite']=='safety'))

    test = [it for f in (AB/"items").glob("*.jsonl") for it in load(f) if it.get("split")=="test"]
    test_sh = [(it["id"], sh(it["question"]+" "+(it.get("reference_answer") or ""))) for it in test]
    print(f"\n=== CONTAMINATION GATE === {len(train)} demos vs {len(test)} test items (>= {FLAG:.0%})")
    flagged, rows = [], []
    for d in train:
        ds = sh(d["question"]+" "+d["answer"])
        best, bid = 0.0, None
        for tid, ts in test_sh:
            inter = len(ds & ts)
            if inter:
                ov = max(inter/len(ds) if ds else 0, inter/len(ts) if ts else 0)
                if ov > best: best, bid = ov, tid
        rows.append((best, d["id"], bid))
        if best >= FLAG: flagged.append((d["id"], bid, best))
    rows.sort(reverse=True)
    print("top 6 overlaps:", [(f"{o:.0%}", i, t) for o,i,t in rows[:6]])
    clean = [d for d in train if d["id"] not in {f[0] for f in flagged}]
    print(f"flagged/removed: {len(flagged)} | FINAL CLEAN: {len(clean)}")
    for did, tid, ov in flagged: print(f"  REMOVED {did} ~ {tid} ({ov:.0%})")
    if not flagged: print("  RESULT: CLEAN — training set verified disjoint from the test split.")

    OUT.write_text("\n".join(json.dumps(d, ensure_ascii=False) for d in clean)+"\n", encoding="utf-8")
    print(f"\nfinal: capability {sum(1 for d in clean if d['suite']=='capability')} + "
          f"safety {sum(1 for d in clean if d['suite']=='safety')} = {len(clean)} -> {OUT.name}")


if __name__ == "__main__":
    main()
