"""Criterion-1 independent recheck: re-parse the `response` the judge actually saw, resolve
every citation, and confirm ZERO ungrounded cites are shown — and that this independent
recount AGREES with the stored shown_fabrications metric. Also asserts every ✓/~ footer line
kept names a citation that truly resolves GROUNDED (no bad line slipped through).

Usage:
    python recount_check.py [path/to/verified_*_results.jsonl]
Default path is the test-split verified results. Exit 0 iff recount==0, metric==0, agree.
"""
from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from citation_index import CitationIndex, Resolution  # noqa: E402
from citation_parser import parse  # noqa: E402
from verifier import shown_ungrounded, UNGROUNDED_RES  # noqa: E402

_AB = Path(os.environ.get("ASSURANCEBENCH")
           or Path(__file__).resolve().parents[2].parent / "assurancebench")
DEFAULT = _AB / "runs" / "verified_ollama_auditlm-run2_test_results.jsonl"
GROUNDED = {Resolution.GROUNDED_EXACT.value, Resolution.GROUNDED_BASE.value}
_IDX = CitationIndex.load_or_build()
_KEEP_LINE = re.compile(r"^\s*[✓~]\s+(.*?)\s+—")   # footer lines that NAME a kept citation


def independent_recount(text: str) -> int:
    """Mirror of the user's recheck: re-parse, exclude honest-disclaimer spans, count all
    ungrounded (stub + family-absent + unrecognized) present in the shown text."""
    n = 0
    for sp in parse(text):
        if sp.honest_disclaimer:
            continue
        if _IDX.resolve(sp.citation_normalized).value in UNGROUNDED_RES:
            n += 1
    return n


def main(path: Path = DEFAULT) -> int:
    rows = [json.loads(l) for l in path.read_text().splitlines() if l.strip()]
    tot_recount = tot_metric = recompute_disagree = stored_lies = 0
    bad_kept = []
    for r in rows:
        resp = r["response"] or ""
        rc = independent_recount(resp)            # mirror of the external recheck
        mt = shown_ungrounded(resp, _IDX)         # the metric's own logic, on the shown text
        tot_recount += rc
        tot_metric += mt
        if rc != mt:
            recompute_disagree += 1
        # the ORIGINAL bug class: the stored field claiming 0 about a string that leaks.
        stored = (r.get("verification") or {}).get("shown_fabrications")
        if stored is not None and stored != mt:
            stored_lies += 1
        # check #3: every NAMED kept footer line must resolve GROUNDED
        fi = resp.find("— Verification —")
        for line in (resp[fi:].splitlines() if fi >= 0 else []):
            m = _KEEP_LINE.match(line)
            if m and _IDX.resolve(m.group(1)).value not in GROUNDED:
                bad_kept.append((r["id"], m.group(1), _IDX.resolve(m.group(1)).value))
    ok = (tot_recount == 0 and tot_metric == 0 and recompute_disagree == 0
          and stored_lies == 0 and not bad_kept)
    print(f"items: {len(rows)} | independent recount (ungrounded shown): {tot_recount} | "
          f"metric sum: {tot_metric} | recount==metric: {recompute_disagree == 0}")
    print(f"stored shown_fabrications disagreeing with the shown text (the original bug): {stored_lies}")
    print(f"check #3 — kept ✓/~ cites resolving ungrounded: {len(bad_kept)} {bad_kept[:5] or '(none)'}")
    print("RESULT:", "PASS — zero ungrounded shown; metric measured on & agrees with the shown text"
          if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    p = Path(sys.argv[1]) if len(sys.argv) > 1 else DEFAULT
    raise SystemExit(main(p))
