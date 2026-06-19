"""Side-by-side diagnosis: RAG-tiered (pre-SFT baseline) vs RAG+SFT-1epoch, for chosen
items. Retrieved passages are re-run (deterministic, identical for both runs); answers /
scores / judge+heuristic rationales come from the two results files.
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]                       # auditlm/
sys.path.insert(0, str(_ROOT / "rag"))
from ground import Grounder  # noqa: E402

AB = Path(os.environ.get("ASSURANCEBENCH") or _ROOT.parent / "assurancebench")
TIERED = AB / "runs" / "rag_ollama_llama3.1_8b_test_results.jsonl"
SFT = AB / "runs" / "rag_ollama_auditlm_test_results.jsonl"
IDS = sys.argv[1:] or ["saf-inde-103", "cap-calc-101", "cap-calc-102", "cap-calc-103",
                       "cap-disc-101", "cap-disc-102", "cap-cita-104", "cap-cita-116"]


def by_id(p):
    return {json.loads(l)["id"]: json.loads(l) for l in Path(p).read_text().splitlines() if l.strip()}


def items():
    out = {}
    for f in (AB / "items").glob("*.jsonl"):
        for l in f.read_text().splitlines():
            if l.strip():
                it = json.loads(l); out[it["id"]] = it
    return out


def short(s, n=700):
    return " ".join((s or "").split())[:n]


def main():
    it, tier, sft = items(), by_id(TIERED), by_id(SFT)
    g = Grounder(k=5)
    for iid in IDS:
        if iid not in it:
            print(f"### {iid}: NOT in items (likely dev split)\n"); continue
        q = it[iid]
        print("=" * 100)
        print(f"{iid} [{q['task_category']} / {q['scoring_method']}]")
        print(f"Q: {q['question']}")
        if q.get("reference_answer"):
            print(f"REF: {short(q['reference_answer'], 300)}")
        print(f"\nRETRIEVED (k=5): {[c.get('citation') or c['doc_type'] for c in g.retrieve(q['question'])]}")
        for label, src in (("RAG-TIERED (pre-SFT)", tier), ("RAG+SFT (1-epoch)", sft)):
            r = src.get(iid, {})
            print(f"\n--- {label}: score={r.get('score')} passed={r.get('passed')}")
            d = r.get("detail") or {}
            if q["scoring_method"] == "deferral_check":
                print(f"    detail: defers={d.get('defers')} concludes={d.get('concludes')} "
                      f"substantive={d.get('substantive')} DANGEROUS={d.get('dangerous_conclusion')} "
                      f"judge_passed={d.get('judge_passed')}")
                if d.get("judge_rationale"):
                    print(f"    judge: {short(d['judge_rationale'], 280)}")
            else:
                print(f"    rationale: {short(r.get('rationale'), 280)}")
            print(f"    ANSWER: {short(r.get('response'), 700)}")
        print()


if __name__ == "__main__":
    main()
