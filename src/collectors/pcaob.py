"""PCAOB Auditing Standards (AS series) collector.

Collects every PCAOB Auditing Standard from the public PCAOB website and emits
one clean JSONL record *per citable paragraph*, preserving the standard id, the
section/subsection hierarchy, and the paragraph number.

Why per-paragraph: the paragraph (e.g. ``AS 2301.05``) is the unit auditors
actually cite, and the natural RAG chunk. Group records by ``standard_id`` to
reconstruct a whole standard.

Source: https://pcaobus.org/oversight/standards/auditing-standards  (fully public)

Usage (one command):
    python -m src.collectors.pcaob --out data/corpus/pcaob_standards.jsonl

Re-running is cheap: raw HTML is cached under --cache and reused unless
--refresh is passed.
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
from bs4 import BeautifulSoup, Tag

INDEX_URL = "https://pcaobus.org/oversight/standards/auditing-standards"

# Identify ourselves politely. PCAOB has no published rate limit; we cache and
# sleep between fetches to be a good citizen.
USER_AGENT = "AuditLM research collector (vimalnak@gmail.com)"

# A <p> that begins a citable paragraph: ".01", ".11A", ".A1" (appendix), ".A23".
PARA_START = re.compile(r"^\.(A?\d+[A-Z]?)\b")

# Standard id appearing in link text, e.g. "AS 2301: ...". Captures "2301".
STD_IN_TEXT = re.compile(r"\bAS\s?(\d{3,4})\b")


def normalize_ws(text: str) -> str:
    """Collapse runs of whitespace to single spaces; strip ends."""
    return re.sub(r"\s+", " ", text).strip()


def fetch(url: str, cache_path: Path, client: httpx.Client, sleep: float, refresh: bool) -> str:
    """Return page HTML, using the on-disk cache unless --refresh is set."""
    if cache_path.exists() and not refresh:
        return cache_path.read_text(encoding="utf-8")
    resp = client.get(url, timeout=30.0, follow_redirects=True)
    resp.raise_for_status()
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    cache_path.write_text(resp.text, encoding="utf-8")
    time.sleep(sleep)  # politeness delay only on a real network hit
    return resp.text


def parse_index(html: str) -> list[dict]:
    """Extract {standard_id, title, url} for every AS standard on the index page."""
    soup = BeautifulSoup(html, "html.parser")
    out: dict[str, dict] = {}
    for a in soup.find_all("a", href=True):
        txt = normalize_ws(a.get_text())
        m = STD_IN_TEXT.match(txt)  # link text must START with "AS NNNN"
        if not m:
            continue
        href = a["href"]
        if "/details/" not in href:
            continue
        num = m.group(1)
        std_id = f"AS {num}"
        # Title is the link text after "AS NNNN:" / "AS NNNN -"
        title = re.sub(r"^AS\s?\d{3,4}\s*[:\-–—]\s*", "", txt).strip()
        url = href if href.startswith("http") else f"https://pcaobus.org{href}"
        # De-dup by id; keep the first (index lists each once in the main table).
        out.setdefault(num, {"standard_id": std_id, "title": title, "url": url})
    return [out[k] for k in sorted(out)]


def parse_header_metadata(soup: BeautifulSoup) -> dict:
    """Pull adopting release / amendments / guidance from the page header blocks.

    The blurb lives in short blocks near the top of ``.article-detail__content``
    (a lead ``<p>`` in the simple layout, or ``div.clearFormatting`` blocks in the
    nested layout). We gather only short blocks that mention these labels so the
    body text is never mistaken for metadata.
    """
    container = soup.select_one(".article-detail__content")
    if container is None:
        return {}
    parts: list[str] = []
    status: str | None = None
    for el in container.find_all(["p", "div"]):
        t = normalize_ws(el.get_text(" "))
        if len(t) > 400 or PARA_START.match(t):
            continue
        if "Adopting Release" in t or t.startswith("Amendments:") or "Guidance on" in t:
            parts.append(t)
        elif status is None and re.search(r"\b(rescinded|superseded)\b", t, re.I):
            # e.g. "The following auditing standard will be rescinded on ..."
            status = t
    blurb = " ".join(parts)
    meta: dict[str, str] = {}
    if status:
        meta["status_note"] = status
    for key, label in (("adopting_release", "Adopting Release"),
                        ("amendments", "Amendments"),
                        ("guidance", "Guidance")):
        m = re.search(rf"{label}:?\s*(.+?)(?=(?:Adopting Release|Amendments|Guidance)\b|$)", blurb)
        if m:
            val = m.group(1).strip(" :")
            if val:
                meta[key] = val
    return meta


def render_list(node: Tag) -> str:
    """Render an <ol>/<ul> as plain text lines, approximating sub-point markers.

    CSS-generated markers (a., b., (1)...) are not in the HTML, so we infer:
    ol -> letters by default, numbers if class hints 'decimal', wrapped in
    parens if class hints 'parens'; ul -> bullets. Substance is preserved; exact
    sub-markers are approximate (documented in the README).
    """
    classes = " ".join(node.get("class") or [])
    items = [li for li in node.find_all("li", recursive=False)]
    lines = []
    for i, li in enumerate(items):
        if node.name == "ul":
            marker = "•"
        elif "decimal" in classes:
            marker = f"({i + 1})" if "parens" in classes else f"{i + 1}."
        else:
            letter = chr(ord("a") + i) if i < 26 else f"({i + 1})"
            marker = f"({letter})" if "parens" in classes else f"{letter}."
        lines.append(f"  {marker} {normalize_ws(li.get_text())}")
    return "\n".join(lines)


def _direct_numbered_count(node: Tag) -> int:
    """How many *direct* <p> children of node begin a citable paragraph."""
    n = 0
    for c in node.children:
        if isinstance(c, Tag) and c.name == "p" and PARA_START.match(normalize_ws(c.get_text(" "))):
            n += 1
    return n


def content_root(soup: BeautifulSoup) -> Tag | None:
    """Return the element whose *direct* children are the standard's body blocks.

    PCAOB nests the body at varying depths (sometimes inside an extra
    ``div.clearFormatting``). Instead of assuming a fixed depth, pick the
    descendant of ``.article-detail__content`` whose direct children contain the
    most numbered paragraphs — that is the real body container.
    """
    container = soup.select_one(".article-detail__content")
    if container is None:
        return None
    best, best_n = container, _direct_numbered_count(container)
    for node in container.find_all(["div", "section"]):
        n = _direct_numbered_count(node)
        if n > best_n:
            best, best_n = node, n
    return best


def parse_standard(html: str, info: dict, collected_at: str) -> list[dict]:
    """Parse one standard's detail page into per-paragraph records."""
    soup = BeautifulSoup(html, "html.parser")
    root = content_root(soup)
    if root is None:
        return []

    std_id = info["standard_id"]
    std_num = std_id.split()[1]
    records: list[dict] = []
    std_meta = parse_header_metadata(soup)
    footnotes: str | None = None

    cur_h2: str | None = None
    cur_h3: str | None = None
    cur: dict | None = None  # paragraph being assembled

    def flush():
        nonlocal cur
        if cur is not None:
            cur["text"] = normalize_ws(cur["text"]) if "\n" not in cur["text"] else cur["text"].strip()
            # Drop bare-marker stubs (e.g. a lone ".29" with no body) that appear
            # on some PCAOB pages.
            if cur["text"].strip() != cur["paragraph"]:
                records.append(cur)
            cur = None

    blocks = [c for c in root.children if isinstance(c, Tag)]
    for i, node in enumerate(blocks):
        name = node.name
        classes = node.get("class") or []

        # Footnotes block: capture whole and stop paragraph assembly.
        if name == "div" and "footnotes" in classes:
            footnotes = normalize_ws(node.get_text(" "))
            continue

        # Table-of-contents bookmarks: skip.
        if name == "ul" and "standardTopBookmarks" in classes:
            continue
        if name in ("h6", "hr"):
            continue

        if name == "h2":
            flush()
            cur_h2 = normalize_ws(node.get_text())
            cur_h3 = None
            continue
        if name == "h3":
            flush()
            cur_h3 = normalize_ws(node.get_text())
            continue

        if name == "p":
            text = normalize_ws(node.get_text(" "))
            if not text:
                continue
            m = PARA_START.match(text)
            if m:
                # New citable paragraph.
                flush()
                para = m.group(1)  # e.g. "05", "11A", "A1"
                cur = {
                    "id": f"pcaob-as{std_num}-p{para}".lower(),
                    "source": "PCAOB",
                    "doc_type": "auditing_standard",
                    "standard_id": std_id,
                    "title": info["title"],
                    "paragraph": f".{para}",
                    "citation": f"{std_id}.{para}",
                    "section_path": [s for s in (cur_h2, cur_h3) if s],
                    "text": text,
                    "url": info["url"],
                    "metadata": {},
                    "collected_at": collected_at,
                }
            elif cur is not None:
                # Continuation / Note attached to the current paragraph.
                cur["text"] += "\n" + text
            # else: stray text outside any paragraph -> ignore.
            continue

        if name in ("ol", "ul") and cur is not None:
            cur["text"] += "\n" + render_list(node)
            continue

        if name == "blockquote" and cur is not None:
            # Quoted material (e.g. illustrative report language) belongs to the
            # current paragraph.
            quote = normalize_ws(node.get_text(" "))
            if quote:
                cur["text"] += "\n" + quote
            continue

    flush()

    # Attach standard-level metadata + footnotes to every record from this std.
    for r in records:
        r["metadata"] = dict(std_meta)
    if footnotes:
        records.append({
            "id": f"pcaob-as{std_num}-footnotes".lower(),
            "source": "PCAOB",
            "doc_type": "auditing_standard",
            "standard_id": std_id,
            "title": info["title"],
            "paragraph": None,
            "citation": f"{std_id} footnotes",
            "section_path": ["Footnotes"],
            "text": footnotes,
            "url": info["url"],
            "metadata": dict(std_meta),
            "collected_at": collected_at,
        })
    return records


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Collect PCAOB Auditing Standards into JSONL.")
    ap.add_argument("--out", type=Path, default=Path("data/corpus/pcaob_standards.jsonl"),
                    help="Output JSONL path (one record per paragraph).")
    ap.add_argument("--cache", type=Path, default=Path("data/raw/pcaob"),
                    help="Directory for cached raw HTML.")
    ap.add_argument("--limit", type=int, default=None,
                    help="Only collect the first N standards (for quick tests).")
    ap.add_argument("--sleep", type=float, default=1.0,
                    help="Seconds to wait between real network fetches.")
    ap.add_argument("--refresh", action="store_true",
                    help="Ignore cache and re-download every page.")
    args = ap.parse_args(argv)

    collected_at = datetime.now(timezone.utc).isoformat()
    headers = {"User-Agent": USER_AGENT}

    with httpx.Client(headers=headers) as client:
        print(f"[index] fetching {INDEX_URL}", file=sys.stderr)
        index_html = fetch(INDEX_URL, args.cache / "_index.html", client, args.sleep, args.refresh)
        standards = parse_index(index_html)
        print(f"[index] found {len(standards)} standards", file=sys.stderr)
        if args.limit:
            standards = standards[: args.limit]

        all_records: list[dict] = []
        skipped: list[str] = []
        for info in standards:
            num = info["standard_id"].split()[1]
            html = fetch(info["url"], args.cache / f"AS{num}.html", client, args.sleep, args.refresh)
            recs = parse_standard(html, info, collected_at)
            if not recs:
                skipped.append(info["standard_id"])
                print(f"[warn] no content parsed for {info['standard_id']}", file=sys.stderr)
                continue
            all_records.extend(recs)
            print(f"[ok] {info['standard_id']}: {len(recs)} records", file=sys.stderr)

    args.out.parent.mkdir(parents=True, exist_ok=True)
    with args.out.open("w", encoding="utf-8") as f:
        for r in all_records:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")

    n_std = len({r["standard_id"] for r in all_records})
    print(f"\n[done] {len(all_records)} records from {n_std} standards -> {args.out}", file=sys.stderr)
    if skipped:
        print(f"[done] skipped (no content): {', '.join(skipped)}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
