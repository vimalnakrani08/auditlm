"""Regenerate the PLAIN (calc + comparison) demo answers in direct question->answer format
(NO retrieved passages, NO 'use the passages' framing) so SFT does not teach over-grounding
on these tasks. Opus answer-only re-calls on the EXISTING questions; citations corpus-verified.

    ANTHROPIC_API_KEY=... rag/.venv/bin/python regen_plain.py        # ~38 calls (~$2-3)
    rag/.venv/bin/python regen_plain.py --mock                       # offline flow test
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(Path(__file__).resolve().parent))
import corpus_verify as cv          # noqa: E402
import teacher as T                  # noqa: E402

DATA = ROOT / "training" / "data"
QS = DATA / "capability_run2_plain_questions.jsonl"
OUT = DATA / "capability_run2_plain.jsonl"

SYSTEM = ("You are an assistant for US external audit (PCAOB standards) and US "
          "GAAP accounting. Answer precisely and cite standards by their identifiers "
          "(e.g. AS 2301.05, ASC 606, 17 CFR 210.2-01) where relevant.")

PLAIN_SYSTEM = (
    "You are a senior US external-audit technical partner writing the model answer for a "
    "training dataset. There are NO source passages — answer directly and correctly from your "
    "own expertise.\n"
    "- CALCULATIONS: use the specific numbers and percentages GIVEN in the question; show the "
    "computation and state the result. Do NOT substitute a different rule, threshold, or "
    "percentage for a value the question provides.\n"
    "- COMPARISONS: give the precise distinction — the discriminating criteria — correctly and "
    "concisely.\n"
    "- Be the answer a Big Four technical partner would give. Cite a governing standard by its "
    "identifier only when you are confident, using only PCAOB AS / SEC (Reg S-X/S-K, SABs) / "
    "FASB ASC / GAGAS; do NOT reference AICPA AU-C, legacy AU, SAS, IFRS, IAS, or ISA.\n"
    "- Write directly for the user; do not mention 'passages'.")


def load_done():
    return {json.loads(l)["id"] for l in OUT.read_text().splitlines() if l.strip()} if OUT.exists() else set()


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--mock", action="store_true")
    ap.add_argument("--no-resume", action="store_true")
    args = ap.parse_args(argv)
    teacher = T.mock_opus if args.mock else T.opus
    idx = cv.build_index()
    qs = [json.loads(l) for l in QS.read_text().splitlines() if l.strip()]
    if args.no_resume and OUT.exists():
        OUT.unlink()
    done = set() if args.no_resume else load_done()
    todo = [q for q in qs if q["id"] not in done]
    print(f"plain regen: {len(qs)} questions | {len(done)} done | {len(todo)} to do", flush=True)

    dropped = []
    with open(OUT, "a", encoding="utf-8") as f:
        for i, q in enumerate(todo, 1):
            ans = teacher(PLAIN_SYSTEM, q["question"], max_tokens=700)
            v = cv.verify_text(ans, idx)
            if not v["clean"]:
                dropped.append((q["id"], v["unverifiable"]));
                print(f"  [{i}/{len(todo)}] {q['id']} {q['task_category']:26} DROP {v['unverifiable']}", flush=True)
                continue
            rec = {"id": q["id"], "suite": "capability", "task_category": q["task_category"],
                   "question": q["question"], "answer": ans, "format": "plain",
                   "messages": [{"role": "system", "content": SYSTEM},
                                {"role": "user", "content": q["question"]},
                                {"role": "assistant", "content": ans}],
                   "verified_citations": v["verified"], "provenance": "opus-plain-corpus-verified"}
            f.write(json.dumps(rec, ensure_ascii=False) + "\n"); f.flush()
            print(f"  [{i}/{len(todo)}] {q['id']} {q['task_category']:26} clean {v['verified']}", flush=True)

    clean = [json.loads(l) for l in OUT.read_text().splitlines() if l.strip()]
    print(f"\nplain regenerated: clean={len(clean)} | dropped(unverifiable)={len(dropped)} -> {OUT.name}")


if __name__ == "__main__":
    raise SystemExit(main())
