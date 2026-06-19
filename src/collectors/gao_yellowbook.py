"""GAO Yellow Book collector — Generally Accepted Government Auditing Standards.

Collects GAGAS (the "Yellow Book"), the U.S. Government Accountability Office's
standards for government audits. One JSONL record **per numbered paragraph**
(e.g. GAGAS 3.87) — the citable unit, the government-audit parallel to the PCAOB
AS paragraphs.

Source: the public GAO Yellow Book PDF. The standards text is published only as a
PDF (the HTML pages are product/landing pages, and the GAO innovations HTML copy
is access-controlled), so this collector parses the PDF directly. We use the
**2018 revision (GAO-21-368G)** — the established, machine-readable edition; the
2024 revision is not yet available from GAO as a cleanly extractable full PDF
(its linked file is a short, poorly-encoded amendment document).

  https://www.gao.gov/assets/gao-21-368g.pdf   (public, free)

Usage (one command):
    python -m src.collectors.gao_yellowbook --out data/corpus/gao_yellowbook.jsonl

The raw PDF is cached under data/raw/gao/; re-runs reuse it (pass --refresh).
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import httpx
import pypdf

USER_AGENT = "AuditLM research collector (vimalnak@gmail.com)"
PDF_URL = "https://www.gao.gov/assets/gao-21-368g.pdf"
EDITION = "GAO-21-368G (2018 revision)"

# Page furniture repeated on every page; dropped before paragraphs are assembled.
_PAGE_FOOTER = re.compile(r"\s*Page\s+\d+\s+GAO-21-368G", re.I)
_RUNNING_HEAD = re.compile(r"\s*Chapter\s+\d+:", re.I)
# A paragraph begins at a line that starts with its number ("3.87 ") followed by
# the standard's text. Cross-references ("see paragraph 3.45") sit mid-line and
# are not matched.
_PARA_START = re.compile(r"^(\d)\.(\d{2,3})\s+\S")
_CHAPTER = re.compile(r"Chapter\s+(\d+):\s*([A-Z][A-Za-z ,]+?)(?:\s+\d+)?\s*(?:\n|$)")


def fetch_pdf(url: str, cache_path: Path, sleep: float, refresh: bool) -> bytes:
    if cache_path.exists() and not refresh:
        return cache_path.read_bytes()
    resp = httpx.get(url, headers={"User-Agent": USER_AGENT}, timeout=180.0,
                     follow_redirects=True)
    resp.raise_for_status()
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    cache_path.write_bytes(resp.content)
    time.sleep(sleep)
    return resp.content


def read_lines(pdf_path: Path) -> tuple[list[str], dict[str, str]]:
    """Return (content lines without page furniture, chapter -> title)."""
    reader = pypdf.PdfReader(str(pdf_path))
    chapter_titles: dict[str, str] = {}
    lines: list[str] = []
    for page in reader.pages:
        text = page.extract_text() or ""
        for m in _CHAPTER.finditer(text):
            chapter_titles.setdefault(m.group(1), re.sub(r"\s+", " ", m.group(2)).strip())
        for ln in text.split("\n"):
            ln = ln.strip()
            if not ln or _PAGE_FOOTER.match(ln) or _RUNNING_HEAD.match(ln):
                continue
            lines.append(ln)
    return lines, chapter_titles


def parse_paragraphs(lines: list[str]) -> list[tuple[str, str, str]]:
    """Return (paragraph, chapter, text) per GAGAS paragraph.

    Each paragraph number appears twice — once in the detailed table of contents
    (a short entry) and once as the real paragraph (the full standard). We collect
    both, then keep, per number, the occurrence with the longest body, which is the
    real paragraph; the TOC entries fall away.
    """
    cand = [(i, f"{m.group(1)}.{m.group(2)}", m.group(1))
            for i, ln in enumerate(lines) for m in [_PARA_START.match(ln)] if m]
    best: dict[str, tuple[int, str, str]] = {}
    for k, (i, label, ch) in enumerate(cand):
        end = cand[k + 1][0] if k + 1 < len(cand) else len(lines)
        body = re.sub(r"\s+", " ", " ".join(lines[i:end])).strip()
        if label not in best or len(body) > len(best[label][1]):
            best[label] = (i, body, ch)
    out = []
    for label, (i, body, ch) in best.items():
        out.append((label, ch, body))
    # document order: by chapter then paragraph number
    out.sort(key=lambda r: (int(r[1]), int(r[0].split(".")[1])))
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Collect the GAO Yellow Book (GAGAS).")
    ap.add_argument("--out", type=Path, default=Path("data/corpus/gao_yellowbook.jsonl"))
    ap.add_argument("--cache", type=Path, default=Path("data/raw/gao"))
    ap.add_argument("--sleep", type=float, default=1.0)
    ap.add_argument("--refresh", action="store_true")
    args = ap.parse_args(argv)

    collected_at = datetime.now(timezone.utc).isoformat()
    try:
        blob = fetch_pdf(PDF_URL, args.cache / "yellowbook-2018.pdf", args.sleep, args.refresh)
    except httpx.HTTPError as e:
        print(f"[network] failed to fetch Yellow Book PDF: {e}", file=sys.stderr)
        return 1
    (args.cache / "yellowbook-2018.pdf").write_bytes(blob)  # ensure cached for pypdf

    lines, chapter_titles = read_lines(args.cache / "yellowbook-2018.pdf")
    paragraphs = parse_paragraphs(lines)

    records = []
    for label, ch, text in paragraphs:
        if len(text) < 40:
            continue
        records.append({
            "id": f"gagas-{label.replace('.', '-')}",
            "source": "GAO",
            "doc_type": "government_auditing_standard",
            "standard": "GAGAS",
            "chapter": ch,
            "chapter_title": chapter_titles.get(ch),
            "paragraph": label,
            "citation": f"GAGAS {label}",
            "text": text,
            "url": PDF_URL,
            "metadata": {"edition": EDITION},
            "collected_at": collected_at,
        })

    args.out.parent.mkdir(parents=True, exist_ok=True)
    with args.out.open("w", encoding="utf-8") as f:
        for r in records:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")

    by_ch: dict[str, int] = {}
    for r in records:
        by_ch[r["chapter"]] = by_ch.get(r["chapter"], 0) + 1
    print(f"[done] {len(records)} GAGAS paragraphs -> {args.out}", file=sys.stderr)
    print(f"[info] per chapter: {dict(sorted(by_ch.items(), key=lambda x: int(x[0])))}",
          file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
