"""Run-2 capability rework (no training, no Opus here):
  - GROUNDED categories -> REFORMAT: swap the tiered policy for the guardrail-augmented one
    in the user turn (keep the existing Opus answer). Free.
  - PLAIN categories (calc, comparison) -> staged for PLAIN regeneration (Opus, separate
    script regen_plain.py): write their questions out.
Keeps all 373 capability demos (no subsample).
"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from collections import Counter

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "rag"))
from ground import TIERED_POLICY, TIERED_GUARDRAIL_POLICY  # noqa: E402

DATA = ROOT / "training" / "data"
SRC = DATA / "train.jsonl"          # 387: 373 capability + 14 (run-1) safety
GROUNDED_OUT = DATA / "capability_run2_grounded.jsonl"
PLAIN_QS = DATA / "capability_run2_plain_questions.jsonl"

GROUNDED_CATS = {"citation_lookup", "disclosure_review", "procedure_suggestion",
                 "filing_summarization", "concept_explanation", "document_drafting",
                 "analytical_flagging"}
PLAIN_CATS = {"calculation_support", "comparison_differentiation"}


def main():
    cap = [r for r in (json.loads(l) for l in SRC.read_text().splitlines() if l.strip())
           if r["suite"] == "capability"]
    grounded, plain, bad = [], [], 0
    for r in cap:
        if r["task_category"] in GROUNDED_CATS:
            user = r["messages"][1]["content"]
            if TIERED_POLICY not in user:                 # safety: must be a tiered grounded prompt
                bad += 1; continue
            new_user = user.replace(TIERED_POLICY, TIERED_GUARDRAIL_POLICY, 1)
            r = {**r, "messages": [r["messages"][0], {"role": "user", "content": new_user},
                                   r["messages"][2]], "format": "grounded_guardrail"}
            grounded.append(r)
        elif r["task_category"] in PLAIN_CATS:
            plain.append({"id": r["id"], "task_category": r["task_category"],
                          "question": r["question"]})       # regenerated plainly by Opus

    GROUNDED_OUT.write_text("\n".join(json.dumps(r, ensure_ascii=False) for r in grounded) + "\n",
                            encoding="utf-8")
    PLAIN_QS.write_text("\n".join(json.dumps(r, ensure_ascii=False) for r in plain) + "\n",
                        encoding="utf-8")

    print(f"capability demos: {len(cap)} | grounded(reformat)={len(grounded)} | "
          f"plain(regen)={len(plain)} | reformat-skipped(not tiered)={bad}")
    # confirm the guardrail is actually present in every reformatted grounded demo
    has_g = sum(1 for r in grounded if "USE GIVEN INPUTS" in r["messages"][1]["content"])
    print(f"guardrail present in grounded user turns: {has_g}/{len(grounded)}")
    print("\nper-category (format | count):")
    by = Counter((r["task_category"], "grounded") for r in grounded)
    by.update(Counter((r["task_category"], "plain") for r in plain))
    for (c, fmt), n in sorted(by.items()):
        print(f"  {c:28} {fmt:8} {n}")
    print(f"\ngrounded -> {GROUNDED_OUT.name}; plain questions -> {PLAIN_QS.name} "
          f"(regenerate with regen_plain.py)")


if __name__ == "__main__":
    main()
