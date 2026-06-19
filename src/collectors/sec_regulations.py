"""SEC regulations collector — Regulation S-X and Regulation S-K (17 CFR).

Collects the two SEC regulations that auditors and preparers cite constantly:

  - Regulation S-X  (17 CFR part 210) — form and content of financial statements
  - Regulation S-K  (17 CFR part 229) — non-financial disclosure requirements

One clean JSONL record **per CFR section**, preserving the section number — the
citation key (e.g. ``17 CFR 210.2-01``), the accounting/disclosure-side parallel
to the PCAOB AS numbers and FASB ASC topics. Getting these numbers right is what
lets the eventual model cite the regulations correctly.

Source: the GovInfo (U.S. GPO) CFR bulk data — the official, freely-redistributable
annual CFR edition in XML. Both parts live in Title 17, volume 3. We use GovInfo
rather than the eCFR ``full`` API because that endpoint throttles to 503; GovInfo
serves the same authoritative text reliably and without bot protection.

  https://www.govinfo.gov/bulkdata/CFR/<year>/title-17/CFR-<year>-title17-vol3.xml

Usage (one command):
    python -m src.collectors.sec_regulations --out data/corpus/sec_regulations.jsonl

The raw XML is cached under data/raw/cfr/; re-runs reuse it (pass --refresh to
re-download). The committed corpus is reproducible from this one public file.
"""

from __future__ import annotations

import argparse
import html
import json
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import httpx

USER_AGENT = "AuditLM research collector (vimalnak@gmail.com)"

# GovInfo CFR bulk XML — Title 17, volume 3 holds parts ~200-239 (S-X = 210,
# S-K = 229). The annual edition lags eCFR "current" by a few months; the edition
# year is recorded in each record's metadata.
GOVINFO_VOL_URL = "https://www.govinfo.gov/bulkdata/CFR/{year}/title-17/CFR-{year}-title17-vol3.xml"
# Human-readable provenance link per section (Title 17, Chapter II is the SEC).
ECFR_SECTION_URL = "https://www.ecfr.gov/current/title-17/chapter-II/part-{part}/section-{section}"

REG_NAMES = {"210": "Regulation S-X", "229": "Regulation S-K"}

SECTION_RE = re.compile(r"<SECTION>(.*?)</SECTION>", re.S)
SECTNO_RE = re.compile(r"<SECTNO>(.*?)</SECTNO>", re.S)
SUBJECT_RE = re.compile(r"<SUBJECT>(.*?)</SUBJECT>", re.S)
# Block-level tags whose close should become a line break before tags are stripped.
_BLOCK_CLOSE_RE = re.compile(
    r"</(P|HD|FP|EXTRACT|LI|ROW|NOTE|CITA|HED|SECHD)>", re.I)


def strip_tags(fragment: str) -> str:
    """Plain text of an XML fragment: drop tags, decode entities, collapse spaces."""
    return re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", "", fragment))).strip()


def section_text(fragment: str) -> str:
    """Readable text of a section body, one line per block element."""
    fragment = _BLOCK_CLOSE_RE.sub("\n", fragment)
    fragment = html.unescape(re.sub(r"<[^>]+>", "", fragment))
    lines = (re.sub(r"\s+", " ", ln).strip() for ln in fragment.split("\n"))
    return "\n".join(ln for ln in lines if ln)


def fetch(url: str, cache_path: Path, sleep: float, refresh: bool) -> str:
    """Return response text, using the on-disk cache unless --refresh is set."""
    if cache_path.exists() and not refresh:
        return cache_path.read_text(encoding="utf-8")
    headers = {"User-Agent": USER_AGENT}
    resp = httpx.get(url, headers=headers, timeout=180.0, follow_redirects=True)
    resp.raise_for_status()
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    cache_path.write_text(resp.text, encoding="utf-8")
    time.sleep(sleep)
    return resp.text


def parse_sections(xml: str, parts: set[str]) -> list[dict]:
    """One record per real CFR section in the requested parts.

    Skips [Reserved] sections and reserved *ranges* ("210.3-07—210.3-08"), which
    are placeholders with no text, and de-duplicates by section number (keeping the
    longest body) since a section can appear more than once in the source.
    """
    by_section: dict[str, dict] = {}
    for m in SECTION_RE.finditer(xml):
        block = m.group(1)
        no_m = SECTNO_RE.search(block)
        if not no_m:
            continue
        section = strip_tags(no_m.group(1)).lstrip("§").strip()
        part = section.split(".")[0]
        if part not in parts:
            continue
        # A reserved range ("210.3-07—210.3-08") is a placeholder, not a section.
        if "—" in section or "–" in section:
            continue
        sub_m = SUBJECT_RE.search(block)
        title = strip_tags(sub_m.group(1)) if sub_m else None
        body = block
        body = body.replace(no_m.group(0), "")
        if sub_m:
            body = body.replace(sub_m.group(0), "")
        text = section_text(body)
        if (title or "").lower().strip("[]. ") == "reserved" or len(text) < 40:
            continue  # reserved/empty placeholder — honest skip, not a fragment
        if section not in by_section or len(text) > len(by_section[section]["text"]):
            by_section[section] = {"part": part, "section": section,
                                   "title": title, "text": text}
    return [by_section[k] for k in sorted(by_section)]


def build_records(parsed: list[dict], year: str, source_url: str,
                  collected_at: str) -> list[dict]:
    records = []
    for p in parsed:
        section, part = p["section"], p["part"]
        records.append({
            "id": f"sec-cfr-{section.replace('.', '-')}".lower(),
            "source": "SEC",
            "doc_type": "regulation",
            "regulation": REG_NAMES[part],
            "cfr_part": part,
            "section": section,
            "citation": f"17 CFR {section}",
            "title": p["title"],
            "text": p["text"],
            "url": ECFR_SECTION_URL.format(part=part, section=section),
            "metadata": {"cfr_title": "17", "edition": year, "source_file": source_url},
            "collected_at": collected_at,
        })
    return records


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Collect SEC Regulation S-X / S-K from the CFR.")
    ap.add_argument("--out", type=Path, default=Path("data/corpus/sec_regulations.jsonl"))
    ap.add_argument("--cache", type=Path, default=Path("data/raw/cfr"))
    ap.add_argument("--year", default="2024", help="CFR annual edition year (default 2024).")
    ap.add_argument("--parts", nargs="*", default=["210", "229"],
                    help="CFR part numbers to collect (default: 210 S-X, 229 S-K).")
    ap.add_argument("--limit", type=int, default=None,
                    help="Keep only the first N records (for quick validation).")
    ap.add_argument("--sleep", type=float, default=1.0)
    ap.add_argument("--refresh", action="store_true", help="Ignore cache, re-download.")
    args = ap.parse_args(argv)

    collected_at = datetime.now(timezone.utc).isoformat()
    url = GOVINFO_VOL_URL.format(year=args.year)
    try:
        xml = fetch(url, args.cache / f"title17_vol3_{args.year}.xml", args.sleep, args.refresh)
    except httpx.HTTPError as e:
        print(f"[network] failed to fetch {url}: {e}", file=sys.stderr)
        print("[network] if this is a connection/allowlist block (not an HTTP error), "
              "the GovInfo domain may need whitelisting.", file=sys.stderr)
        return 1

    parsed = parse_sections(xml, set(args.parts))
    records = build_records(parsed, args.year, url, collected_at)
    if args.limit:
        records = records[: args.limit]

    args.out.parent.mkdir(parents=True, exist_ok=True)
    with args.out.open("w", encoding="utf-8") as f:
        for r in records:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")

    counts: dict[str, int] = {}
    for r in records:
        counts[r["regulation"]] = counts.get(r["regulation"], 0) + 1
    for reg, n in sorted(counts.items()):
        print(f"[ok] {reg}: {n} sections", file=sys.stderr)
    print(f"[done] {len(records)} records -> {args.out}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
