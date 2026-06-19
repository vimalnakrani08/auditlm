"""FASB ASC topic-number scaffolding from the US-GAAP XBRL taxonomy.

The FASB Accounting Standards Codification (ASC) is the accounting-side citation
system — ASC Topic 606 (Revenue), 842 (Leases), 326 (Credit Losses), and so on —
the exact parallel to the PCAOB AS numbers. The full ASC *text* is licensed and
the free Basic View is behind bot protection, so this collector takes only the
authoritative **topic/subtopic NUMBER structure** from a source that is public
and freely redistributable: the FASB US-GAAP XBRL Financial Reporting Taxonomy.

The taxonomy's reference linkbase maps every US-GAAP element to its ASC reference
(`<codification-part:Topic>606</...>`). We invert that into one record per ASC
Topic — the citation key, the subtopics seen under it, and a few of the taxonomy
elements that cite it (which evidence the topic's subject, e.g. Topic 606 ->
RevenueJudgment, ContractWithCustomerAssetNet). No ASC prose is reproduced.

  https://xbrl.fasb.org/us-gaap/<year>/us-gaap-<year>.zip   (public, no login)

Usage (one command):
    python -m src.collectors.fasb_asc --out data/corpus/fasb_asc_topics.jsonl
"""

from __future__ import annotations

import argparse
import io
import json
import re
import sys
import time
import zipfile
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

import httpx

USER_AGENT = "AuditLM research collector (vimalnak@gmail.com)"
TAXONOMY_URL = "https://xbrl.fasb.org/us-gaap/{year}/us-gaap-{year}.zip"
REF_LINKBASE = "us-gaap-{year}/elts/us-gaap-ref-{year}.xml"
# asc.fasb.org is the canonical (human-viewable) home of each topic.
ASC_TOPIC_URL = "https://asc.fasb.org/{topic}"

# Taxonomy elements that are XBRL structure (dimensions, ASU adoption markers,
# abstract headers) rather than accounting concepts — excluded from the few
# sample elements shown per topic so the topic's subject reads clearly.
_NOISE = re.compile(r"Member$|Axis$|Domain$|Table$|Abstract$|^AccountingStandardsUpdate")


def fetch_zip(url: str, cache_path: Path, sleep: float, refresh: bool) -> bytes:
    if cache_path.exists() and not refresh:
        return cache_path.read_bytes()
    resp = httpx.get(url, headers={"User-Agent": USER_AGENT}, timeout=180.0,
                     follow_redirects=True)
    resp.raise_for_status()
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    cache_path.write_bytes(resp.content)
    time.sleep(sleep)
    return resp.content


def parse_reference_linkbase(xml: str):
    """Return (topic_subtopics, topic_elements) from the US-GAAP reference linkbase.

    topic_subtopics: ASC topic -> sorted list of subtopic numbers seen under it.
    topic_elements:  ASC topic -> set of element names that cite it.
    """
    # reference resource -> ASC (topic, subtopic), keyed by xlink:label
    ref: dict[str, tuple[str, str | None]] = {}
    for m in re.finditer(
            r"<link:reference\b[^>]*xlink:label=['\"]([^'\"]+)['\"][^>]*>(.*?)</link:reference>",
            xml, re.S):
        tp = re.search(r"<codification-part:Topic>(\d+)</codification-part:Topic>", m.group(2))
        if not tp:
            continue
        st = re.search(r"<codification-part:SubTopic>(\d+)</codification-part:SubTopic>", m.group(2))
        ref[m.group(1)] = (tp.group(1), st.group(1) if st else None)
    # locator label -> element name
    loc: dict[str, str] = {}
    for m in re.finditer(
            r"<link:loc\b[^>]*xlink:href=['\"]([^'\"]+)['\"][^>]*xlink:label=['\"]([^'\"]+)['\"]", xml):
        loc[m.group(2)] = m.group(1).split("#")[-1].replace("us-gaap_", "")
    # arc: element locator -> reference resource
    topic_subtopics: dict[str, set] = defaultdict(set)
    topic_elements: dict[str, set] = defaultdict(set)
    for m in re.finditer(
            r"<link:referenceArc\b[^>]*xlink:from=['\"]([^'\"]+)['\"][^>]*xlink:to=['\"]([^'\"]+)['\"]", xml):
        frm, to = m.group(1), m.group(2)
        if frm in loc and to in ref:
            topic, subtopic = ref[to]
            topic_elements[topic].add(loc[frm])
            if subtopic:
                topic_subtopics[topic].add(subtopic)
    return topic_subtopics, topic_elements


def build_records(topic_subtopics, topic_elements, year, source_url, collected_at):
    records = []
    for topic in sorted(topic_elements, key=int):
        elements = topic_elements[topic]
        subtopics = sorted(topic_subtopics.get(topic, []), key=int)
        sample = sorted((e for e in elements if not _NOISE.search(e)), key=len)[:6]
        text = (f"ASC Topic {topic} — accounting-side citation key (FASB Accounting "
                f"Standards Codification). Subtopics: {', '.join(subtopics) or 'n/a'}. "
                f"Referenced by {len(elements)} US-GAAP taxonomy elements, e.g. "
                f"{', '.join(sample)}.")
        records.append({
            "id": f"asc-{topic}",
            "source": "FASB",
            "doc_type": "asc_topic_structure",
            "asc_topic": topic,
            "citation": f"ASC {topic}",
            "subtopics": subtopics,
            "element_count": len(elements),
            "sample_elements": sample,
            "title": None,
            "text": text,
            "url": ASC_TOPIC_URL.format(topic=topic),
            "metadata": {"taxonomy": f"us-gaap-{year}", "source_file": source_url},
            "collected_at": collected_at,
        })
    return records


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Extract ASC topic structure from the US-GAAP taxonomy.")
    ap.add_argument("--out", type=Path, default=Path("data/corpus/fasb_asc_topics.jsonl"))
    ap.add_argument("--cache", type=Path, default=Path("data/raw/fasb_taxonomy"))
    ap.add_argument("--year", default="2024")
    ap.add_argument("--sleep", type=float, default=1.0)
    ap.add_argument("--refresh", action="store_true")
    args = ap.parse_args(argv)

    collected_at = datetime.now(timezone.utc).isoformat()
    url = TAXONOMY_URL.format(year=args.year)
    try:
        blob = fetch_zip(url, args.cache / f"us-gaap-{args.year}.zip", args.sleep, args.refresh)
    except httpx.HTTPError as e:
        print(f"[network] failed to fetch taxonomy {url}: {e}", file=sys.stderr)
        return 1

    with zipfile.ZipFile(io.BytesIO(blob)) as z:
        xml = z.read(REF_LINKBASE.format(year=args.year)).decode("utf-8", errors="replace")
    topic_subtopics, topic_elements = parse_reference_linkbase(xml)
    records = build_records(topic_subtopics, topic_elements, args.year, url, collected_at)

    args.out.parent.mkdir(parents=True, exist_ok=True)
    with args.out.open("w", encoding="utf-8") as f:
        for r in records:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")

    n_sub = sum(len(r["subtopics"]) for r in records)
    print(f"[done] {len(records)} ASC topics ({n_sub} subtopic links) -> {args.out}",
          file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
