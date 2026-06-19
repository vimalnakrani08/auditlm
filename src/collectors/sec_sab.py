"""SEC Staff Accounting Bulletins (SAB) collector.

Collects the SEC staff's accounting guidance — the natural companion to the
Regulation S-X / S-K rules. SABs are staff interpretations codified in 17 CFR
part 211; auditors and preparers cite them by number (e.g. **SAB No. 99**,
Materiality) and by SAB Codification Topic (e.g. Topic 1.M).

One JSONL record per SAB, preserving the SAB number as the citation key and the
SAB Codification Topic in metadata where the bulletin states it.

Source: the public SEC index of Staff Accounting Bulletins and the individual
bulletin pages on sec.gov. The index links some bulletins as HTML and a few as
PDF only; this collector takes the HTML ones and reports the PDF-only bulletins
honestly (PDF parsing is not in scope).

  https://www.sec.gov/rules-regulations/staff-guidance/staff-accounting-bulletins

Usage (one command):
    python -m src.collectors.sec_sab --out data/corpus/sec_sab.jsonl

Raw HTML is cached under data/raw/sab/; re-runs reuse it (pass --refresh).
"""

from __future__ import annotations

import argparse
import json
import io
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import httpx
import pypdf
from bs4 import BeautifulSoup

USER_AGENT = "AuditLM research collector (vimalnak@gmail.com)"

INDEX_URL = "https://www.sec.gov/rules-regulations/staff-guidance/staff-accounting-bulletins"
BASE = "https://www.sec.gov"

# Every SAB carries a "[Release No. SAB NNN]" line right after the site chrome and
# title block; starting there drops all the navigation/.gov boilerplate uniformly
# across both the classic interps pages and the current sec.gov template.
_RELEASE_LINE = re.compile(r"\[?\s*Release\s+No\.\s*SAB", re.I)


def fetch(url: str, cache_path: Path, sleep: float, refresh: bool) -> bytes:
    if cache_path.exists() and not refresh:
        return cache_path.read_bytes()
    resp = httpx.get(url, headers={"User-Agent": USER_AGENT}, timeout=90.0,
                     follow_redirects=True)
    resp.raise_for_status()
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    cache_path.write_bytes(resp.content)
    time.sleep(sleep)
    return resp.content


def parse_index(html: str) -> tuple[list[dict], list[dict]]:
    """Return (html_sabs, pdf_sabs). Each entry: {label, number, suffix, url}."""
    seen: dict[str, str] = {}
    for m in re.finditer(r'href="([^"]+)"[^>]*>\s*(SAB\s*(\d+)([A-Z]?))\s*<', html):
        label = re.sub(r"\s+", " ", m.group(2)).strip()
        seen.setdefault(label, (m.group(1), m.group(3), m.group(4)))
    html_sabs, pdf_sabs = [], []
    for label, (href, number, suffix) in seen.items():
        url = href if href.startswith("http") else BASE + href
        entry = {"label": label, "number": number, "suffix": suffix, "url": url}
        (pdf_sabs if url.lower().endswith(".pdf") else html_sabs).append(entry)
    key = lambda e: (int(e["number"]), e["suffix"])
    return sorted(html_sabs, key=key), sorted(pdf_sabs, key=key)


def lines_from_html(html: bytes) -> tuple[list[str], str]:
    soup = BeautifulSoup(html, "html.parser")
    title = soup.title.get_text(" ", strip=True) if soup.title else ""
    for tag in soup(["script", "style", "nav", "header", "footer", "form", "aside"]):
        tag.decompose()
    lines = [re.sub(r"\s+", " ", ln).strip() for ln in soup.get_text("\n").split("\n")]
    return [ln for ln in lines if ln], title


def lines_from_pdf(pdf: bytes) -> tuple[list[str], str]:
    reader = pypdf.PdfReader(io.BytesIO(pdf))
    lines = []
    for page in reader.pages:
        for ln in (page.extract_text() or "").split("\n"):
            ln = re.sub(r"\s+", " ", ln).strip()
            if ln:
                lines.append(ln)
    return lines, ""  # PDFs have no <title>; subject is taken from the body


def extract_sab(lines: list[str], title: str, sab_id: str) -> tuple[str | None, str | None, str | None]:
    """Return (subject, topic, text) from a SAB's text lines (HTML or PDF)."""
    # Start at the "[Release No. SAB NNN]" line — everything before it is site
    # chrome and a repeated title; everything after is the bulletin itself.
    start = next((i for i, ln in enumerate(lines) if _RELEASE_LINE.search(ln)), None)
    text = "\n".join(lines[start:]).strip() if start is not None else "\n".join(lines).strip()

    # Subject ("Materiality") follows "No. <this SAB> –/:" in the title or body.
    # Require the bulletin's own number and a real title separator (en/em dash or
    # colon, not a hyphen) so a date ("March 18,") or release number is never
    # mistaken for the subject — an honest None beats a wrong title.
    subj_re = re.compile(r"No\.\s*" + re.escape(sab_id) + r"\s*[–—:]\s*([^\n|]{3,90})", re.I)
    sm = subj_re.search(title) or subj_re.search("\n".join(lines))
    subject = re.sub(r"\s+", " ", sm.group(1)).strip().rstrip(".") if sm else None

    # SAB Codification Topic, taken from the bulletin's own codification
    # instruction so it is authoritative, not guessed: "adds Section M to Topic 1"
    # -> 1.M (SAB 99), "Section N to Topic 1" -> 1.N (SAB 108), "adds new major
    # Topic 13" -> 13 (SAB 101). A bare "Topic N" is only trusted next to an
    # add/amend/revise verb, so a passing mention of some other topic is ignored.
    sec = re.search(r"Section\s+([A-Z])\s+to\s+Topic\s+(\d+)", text, re.I) \
        or re.search(r"Topic\s+(\d+)\s*,\s*Section\s+([A-Z])\b", text, re.I)
    if sec:
        a, b = sec.groups()
        topic = f"{b}.{a}" if a.isalpha() else f"{a}.{b}"
    else:
        verb = re.search(r"(?:add|amend|revis|supersed)\w*[^.]{0,40}?Topic\s+(\d+)", text, re.I)
        topic = verb.group(1) if verb else None
    return subject, topic, text


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Collect SEC Staff Accounting Bulletins.")
    ap.add_argument("--out", type=Path, default=Path("data/corpus/sec_sab.jsonl"))
    ap.add_argument("--cache", type=Path, default=Path("data/raw/sab"))
    ap.add_argument("--only", nargs="*", help="Only these SAB numbers (e.g. 99 101 108).")
    ap.add_argument("--sleep", type=float, default=0.5)
    ap.add_argument("--refresh", action="store_true")
    args = ap.parse_args(argv)

    collected_at = datetime.now(timezone.utc).isoformat()
    try:
        index_html = fetch(INDEX_URL, args.cache / "index.html", args.sleep, args.refresh)
    except httpx.HTTPError as e:
        print(f"[network] failed to fetch SAB index: {e}", file=sys.stderr)
        return 1

    html_sabs, pdf_sabs = parse_index(index_html.decode("utf-8", errors="replace"))
    sabs = [(s, "html") for s in html_sabs] + [(s, "pdf") for s in pdf_sabs]
    if args.only:
        want = set(args.only)
        sabs = [(s, kind) for s, kind in sabs if s["number"] in want]

    records, failed = [], []
    for s, kind in sabs:
        sab_id = f"{s['number']}{s['suffix']}"
        try:
            blob = fetch(s["url"], args.cache / f"sab{sab_id}.{kind}", args.sleep, args.refresh)
            lines, title = (lines_from_pdf(blob) if kind == "pdf"
                            else lines_from_html(blob))
        except Exception as e:  # network or PDF-parse failure
            failed.append((s["label"], str(e)))
            continue
        subject, topic, text = extract_sab(lines, title, sab_id)
        if len(text) < 60:
            failed.append((s["label"], "no substantive text extracted"))
            continue
        records.append({
            "id": f"sab-{sab_id}".lower(),
            "source": "SEC",
            "doc_type": "staff_guidance",
            "guidance_type": "Staff Accounting Bulletin",
            "sab_number": sab_id,
            "title": subject,
            "citation": f"SAB No. {sab_id}",
            "text": text,
            "url": s["url"],
            "metadata": {"cfr_part": "211", "topic": topic, "format": kind},
            "collected_at": collected_at,
        })

    records.sort(key=lambda r: (int(re.match(r"\d+", r["sab_number"]).group()), r["sab_number"]))
    args.out.parent.mkdir(parents=True, exist_ok=True)
    with args.out.open("w", encoding="utf-8") as f:
        for r in records:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")

    print(f"[done] {len(records)} SAB records -> {args.out}", file=sys.stderr)
    for label, why in failed:
        print(f"[warn] {label}: {why}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
