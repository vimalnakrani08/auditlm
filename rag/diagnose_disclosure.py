"""Diagnose the disclosure_review RAG regression: for chosen items, show side by side
(a) what RAG retrieved, (b) base-Llama answer vs RAG answer, (c) both scores + judge
rationales. Retrieval is re-run deterministically (no API); answers/scores come from
the two results files.
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

from ground import Grounder

AB = Path(os.environ.get("ASSURANCEBENCH", Path(__file__).resolve().parents[2] / "assurancebench"))
BASE = AB / "runs" / "ollama_llama3.1_8b_test_results.jsonl"
RAG = AB / "runs" / "rag_ollama_llama3.1_8b_test_results.jsonl"
IDS = sys.argv[1:] or ["cap-disc-101", "cap-disc-102", "cap-disc-109", "cap-disc-110", "cap-disc-112"]


def index_by_id(path):
    return {json.loads(l)["id"]: json.loads(l) for l in Path(path).read_text().splitlines() if l.strip()}


def items_by_id():
    out = {}
    for f in (AB / "items").glob("*.jsonl"):
        for l in f.read_text().splitlines():
            if l.strip():
                it = json.loads(l); out[it["id"]] = it
    return out


def short(s, n=320):
    return " ".join((s or "").split())[:n]


def main():
    items, base, rag = items_by_id(), index_by_id(BASE), index_by_id(RAG)
    g = Grounder(k=5)
    for iid in IDS:
        it = items[iid]
        print("=" * 100)
        print(f"{iid}  | {it['question']}")
        print(f"REFERENCE: {short(it.get('reference_answer'), 260)}")
        print(f"\n(a) RAG RETRIEVED (k=5):")
        for i, c in enumerate(g.retrieve(it["question"]), 1):
            cite = c.get("citation") or f"{c['source']}/{c['doc_type']}"
            print(f"   [{i}] {cite:18} ({c['source']:4}) {short(c.get('text'), 110)}")
        b, r = base.get(iid, {}), rag.get(iid, {})
        print(f"\n(b/c) BASE : score={b.get('score')} passed={b.get('passed')}")
        print(f"        ans: {short(b.get('response'), 300)}")
        print(f"        judge: {short(b.get('rationale'), 220)}")
        print(f"      RAG  : score={r.get('score')} passed={r.get('passed')}")
        print(f"        ans: {short(r.get('response'), 300)}")
        print(f"        judge: {short(r.get('rationale'), 220)}")
        print()


if __name__ == "__main__":
    main()
