"""Controlled A/B of the grounding policy on a few items: same retrieval + same base
model, ONLY the policy string changes (old strict vs new tiered). No judge — shows the
answers + lightweight deterministic checks so the prompt change can be reviewed before
a full re-run.
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

from ground import STRICT_POLICY as OLD_POLICY   # "before" arm = strict
from ground import TIERED_POLICY as NEW_POLICY   # "after" arm = tiered
from ground import Grounder

AB = Path(os.environ.get("ASSURANCEBENCH")
          or Path(__file__).resolve().parents[1].parent / "assurancebench")
sys.path.insert(0, str(AB))
import src.models as M                     # noqa: E402
from src.scoring import citation, deferral  # noqa: E402

IDS = sys.argv[1:] or ["cap-disc-101", "cap-disc-109", "cap-cita-101", "saf-inde-101"]


def items_by_id():
    out = {}
    for f in (AB / "items").glob("*.jsonl"):
        for l in f.read_text().splitlines():
            if l.strip():
                it = json.loads(l); out[it["id"]] = it
    return out


def short(s, n=380):
    return " ".join((s or "").split())[:n]


def main():
    items = items_by_id()
    g = Grounder(k=5)
    base = M.from_spec("ollama:llama3.1:8b")
    for iid in IDS:
        it = items[iid]
        passages = g.retrieve(it["question"])
        old = base(g.build_prompt(it["question"], passages, OLD_POLICY))
        new = base(g.build_prompt(it["question"], passages, NEW_POLICY))
        print("=" * 100)
        print(f"{iid}  [{it['task_category']} / {it['scoring_method']}]")
        print(f"Q: {it['question']}")
        print(f"retrieved: {[ (c.get('citation') or c['doc_type']) for c in passages ]}")
        print(f"\n  BEFORE (strict): {short(old)}")
        print(f"\n  AFTER  (tiered): {short(new)}")
        # lightweight checks
        if it["scoring_method"] == "exact_citation":
            exp = it["expected_citations"]; cm = it.get("citation_match", "exact")
            print(f"\n  [citation check] expected={exp} match={cm} | "
                  f"BEFORE pass={citation.score(old, exp, cm).passed} "
                  f"AFTER pass={citation.score(new, exp, cm).passed}")
        if it["scoring_method"] == "deferral_check":
            ho = deferral.heuristic(old, it["deferral_required"])
            hn = deferral.heuristic(new, it["deferral_required"])
            print(f"\n  [safety heuristic] BEFORE passed={ho.passed} dangerous={ho.detail['dangerous_conclusion']} "
                  f"| AFTER passed={hn.passed} dangerous={hn.detail['dangerous_conclusion']}")
        print()


if __name__ == "__main__":
    main()
