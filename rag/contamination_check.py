"""Contamination check — do any AssuranceBench `test` items near-duplicate a corpus
passage? The corpus is source standards/filings (not benchmark items), so retrieving
over it should not leak answers — but we verify it once, before the headline Base+RAG
run, to protect the credibility of that number.

Method: 8-gram (word) shingles. Stage 1 (fast): fraction of each test item's
(question + reference_answer) shingles that appear ANYWHERE in the corpus — an upper
bound. Stage 2: for any item above the bar, find the single passage it most overlaps
(the true near-duplicate measure) and name it.

    python contamination_check.py
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path

from corpus import load_chunks

RAG = Path(__file__).resolve().parent
AB = Path(os.environ.get("ASSURANCEBENCH", RAG.parent.parent / "assurancebench"))
WORD = re.compile(r"[a-z0-9]+")
K = 8
FLAG = 0.40   # >=40% of an item's 8-grams overlapping a single passage = investigate


def shingles(text: str) -> set:
    toks = WORD.findall(text.lower())
    return {hash(tuple(toks[i:i + K])) for i in range(len(toks) - K + 1)}


def load_test_items() -> list[dict]:
    items = []
    for f in sorted((AB / "items").glob("*.jsonl")):
        for line in f.read_text(encoding="utf-8").splitlines():
            if line.strip():
                it = json.loads(line)
                if it.get("split") == "test":
                    items.append(it)
    return items


def main() -> int:
    items = load_test_items()
    chunks = load_chunks()
    print(f"test items: {len(items)} | corpus passages: {len(chunks)}")

    # stage 1: global shingle set (upper bound on any single-passage overlap)
    global_sh: set = set()
    for c in chunks:
        global_sh |= shingles(c.get("text") or "")
    rows = []
    for it in items:
        s = shingles(it["question"] + " " + (it.get("reference_answer") or ""))
        ov = len(s & global_sh) / len(s) if s else 0.0
        rows.append((ov, it["id"], it["task_category"], s))
    rows.sort(key=lambda r: r[0], reverse=True)

    print("\nTop 10 test items by global 8-gram overlap with the corpus (upper bound):")
    for ov, iid, cat, _ in rows[:10]:
        print(f"  {ov:6.1%}  {iid:16} {cat}")

    # stage 2: drill into anything above the bar — find the single most-overlapping passage
    suspects = [r for r in rows if r[0] >= FLAG]
    print(f"\nItems >= {FLAG:.0%} global overlap (candidates for per-passage drill-down): "
          f"{len(suspects)}")
    near_dupes = 0
    for ov, iid, cat, s in suspects:
        best, best_c = 0.0, None
        for c in chunks:
            o = len(s & shingles(c.get("text") or "")) / len(s)
            if o > best:
                best, best_c = o, c
        tag = best_c.get("citation") or f"{best_c['source']}/{best_c['doc_type']}" if best_c else "?"
        flag = "  <-- NEAR-DUPLICATE" if best >= FLAG else ""
        if best >= FLAG:
            near_dupes += 1
        print(f"  {iid:16} best single-passage overlap {best:5.1%} vs [{tag}]{flag}")

    print(f"\nRESULT: {near_dupes} test item(s) near-duplicate a single corpus passage "
          f"(>= {FLAG:.0%}). " + ("CLEAN — no contamination boundary breach."
                                  if near_dupes == 0 else "REVIEW the items above."))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
