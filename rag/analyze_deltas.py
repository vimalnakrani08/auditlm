"""Compare two AssuranceBench result files and report CAPABILITY and SAFETY deltas
SEPARATELY — because RAG may move them in OPPOSITE directions (capability up via
retrieved facts; safety calibration possibly down if authoritative rule text emboldens
the model to conclude rather than defer). Designed for Base -> Base+RAG -> Base+RAG+SFT.

    python analyze_deltas.py <new_results.jsonl> [baseline_results.jsonl]

baseline defaults to the frozen Llama test run. Prints per-category capability means
+ pass rates with deltas, then the safety gate / dangerous-conclusion count / per-zone
with deltas, and a one-line capability-vs-safety read.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

AB = Path(os.environ.get("ASSURANCEBENCH", Path(__file__).resolve().parents[2] / "assurancebench"))
sys.path.insert(0, str(AB))
from src import scorecard  # noqa: E402  (reuse the OFFICIAL safety-gate logic)

CAP_ORDER = ["citation_lookup", "procedure_suggestion", "concept_explanation",
             "disclosure_review", "filing_summarization", "comparison_differentiation",
             "document_drafting", "analytical_flagging", "calculation_support"]
ZONES = ["independence_conclusion", "fraud_accusation", "legal_regulatory_conclusion",
         "novel_transaction_treatment", "tax_position"]


def load(path: Path) -> list[dict]:
    return [json.loads(l) for l in Path(path).read_text(encoding="utf-8").splitlines() if l.strip()]


def cap_stats(results, cat):
    rs = [r for r in results if r["suite"] == "capability" and r["task_category"] == cat
          and r["score"] is not None]
    n = len(rs)
    mean = sum(r["score"] for r in rs) / n if n else None
    pr = sum(bool(r["passed"]) for r in rs) / n if n else None
    return n, mean, pr


def cap_overall(results):
    rs = [r for r in results if r["suite"] == "capability" and r["score"] is not None]
    n = len(rs)
    return (sum(r["score"] for r in rs) / n if n else 0.0), n


def zone_stats(results, zone):
    rs = [r for r in results if r["suite"] == "safety" and r["task_category"] == zone
          and r["score"] is not None]
    n = len(rs)
    mean = sum(r["score"] for r in rs) / n if n else None
    return n, mean


def d(new, old):
    if new is None or old is None:
        return "   n/a"
    return f"{new - old:+.3f}"


def main() -> int:
    new_path = Path(sys.argv[1])
    base_path = Path(sys.argv[2]) if len(sys.argv) > 2 else \
        AB / "runs" / "ollama_llama3.1_8b_test_results.jsonl"
    new, base = load(new_path), load(base_path)
    print(f"NEW : {new_path.name} ({len(new)} items)")
    print(f"BASE: {base_path.name} ({len(base)} items)\n")

    print("=== CAPABILITY (mean score, pass-rate) — delta vs baseline ===")
    print(f"  {'category':27} {'base':>6} {'new':>6} {'delta':>7}   {'base_pr':>7} {'new_pr':>6}")
    for c in CAP_ORDER:
        bn, bm, bpr = cap_stats(base, c)
        nn, nm, npr = cap_stats(new, c)
        bm_s = f"{bm:.3f}" if bm is not None else "  n/a"
        nm_s = f"{nm:.3f}" if nm is not None else "  n/a"
        bpr_s = f"{bpr:.0%}" if bpr is not None else "n/a"
        npr_s = f"{npr:.0%}" if npr is not None else "n/a"
        print(f"  {c:27} {bm_s:>6} {nm_s:>6} {d(nm, bm):>7}   {bpr_s:>7} {npr_s:>6}")
    bo, _ = cap_overall(base)
    no, _ = cap_overall(new)
    print(f"  {'CAPABILITY OVERALL':27} {bo:>6.3f} {no:>6.3f} {d(no, bo):>7}")

    print("\n=== SAFETY — gate / dangerous conclusions / per-zone — delta vs baseline ===")
    bg, ng = scorecard.safety_gate(base), scorecard.safety_gate(new)
    print(f"  gate passed:           base={bg['passed']}   new={ng['passed']}")
    print(f"  DANGEROUS conclusions: base={len(bg['hard_fails'])}   new={len(ng['hard_fails'])}"
          f"   (delta {len(ng['hard_fails']) - len(bg['hard_fails']):+d})  <-- the zero-tolerance signal")
    print(f"  non-dangerous pass:    base={bg['soft_rate']:.0%} ({bg['soft_pass']}/{bg['soft_n']})"
          f"   new={ng['soft_rate']:.0%} ({ng['soft_pass']}/{ng['soft_n']})   (delta {ng['soft_rate'] - bg['soft_rate']:+.0%})")
    if ng["hard_fails"]:
        print(f"  NEW dangerous-conclusion items: {', '.join(r['id'] for r in ng['hard_fails'])}")
    print(f"\n  {'zone':30} {'base':>6} {'new':>6} {'delta':>7}")
    for z in ZONES:
        bn, bm = zone_stats(base, z)
        nn, nm = zone_stats(new, z)
        bm_s = f"{bm:.3f}" if bm is not None else "  n/a"
        nm_s = f"{nm:.3f}" if nm is not None else "  n/a"
        print(f"  {z:30} {bm_s:>6} {nm_s:>6} {d(nm, bm):>7}")

    print("\n=== READ ===")
    cap_delta = no - bo
    saf_delta = ng["soft_rate"] - bg["soft_rate"]
    dang_delta = len(ng["hard_fails"]) - len(bg["hard_fails"])
    print(f"  Capability {cap_delta:+.3f} overall; safety non-dangerous pass {saf_delta:+.0%}; "
          f"dangerous conclusions {dang_delta:+d}.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
