"""Base+RAG smoke-test on a few dev items — shows, per item: (a) the retrieved
passages that go into the prompt, (b) the grounded model answer, (c) the score from
the SAME scoring the runner uses. Exercises retrieval + grounding + the local base
model + scoring end-to-end (one Grounder load). llm_judge items need ANTHROPIC_API_KEY
(score shows 'pending' without it); citation/deferral score with no API.

    /path/to/rag/.venv/bin/python smoketest_rag.py [id ...]
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

from ground import Grounder

AB = Path(os.environ.get("ASSURANCEBENCH", Path(__file__).resolve().parents[2] / "assurancebench"))
sys.path.insert(0, str(AB))
import src.models as M               # noqa: E402
from src.runner import score_item    # noqa: E402

IDS = sys.argv[1:] or ["cap-cita-101", "cap-proc-102", "saf-inde-101"]


def load(ids):
    out = {}
    for f in (AB / "items").glob("*.jsonl"):
        for line in f.read_text(encoding="utf-8").splitlines():
            if line.strip():
                it = json.loads(line)
                if it["id"] in ids:
                    out[it["id"]] = it
    return [out[i] for i in ids if i in out]


def main() -> int:
    items = load(IDS)
    g = Grounder(k=5)
    base = M.from_spec("ollama:llama3.1:8b")   # same base path the rag: adapter wraps

    for it in items:
        print("=" * 90)
        print(f"{it['id']}  [{it['task_category']} / {it['scoring_method']}]")
        print(f"Q: {it['question']}")
        print(f"REFERENCE: {it.get('reference_answer')}")
        prompt, passages = g.ground(it["question"])
        print(f"\n(a) RETRIEVED PASSAGES IN PROMPT (k={len(passages)}):")
        for i, c in enumerate(passages, 1):
            cite = c.get("citation") or f"{c['source']}/{c['doc_type']}"
            print(f"   [{i}] {cite}")
        in_prompt = all(f"[{i}]" in prompt for i in range(1, len(passages) + 1))
        print(f"   -> all {len(passages)} passages present in the prompt: {in_prompt} "
              f"(prompt length {len(prompt)} chars)")
        answer = base(prompt)
        print(f"\n(b) GROUNDED ANSWER:\n   {' '.join(answer.split())[:500]}")
        res = score_item(it, answer)        # exactly the runner's scoring
        print(f"\n(c) SCORE: passed={res['passed']} score={res['score']} "
              f"({res['scoring_method']})\n   rationale: {res['rationale'][:160]}")
        print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
