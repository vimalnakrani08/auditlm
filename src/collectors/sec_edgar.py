"""SEC EDGAR collector — high-value sections from 10-K filings.

Collects annual reports (10-K) from a diverse set of public companies and
isolates the four highest-value text sections for the audit corpus, rather than
dumping whole filings:

  - auditor_report  — the "Report of Independent Registered Public Accounting Firm"
  - critical_audit_matter — CAMs disclosed within the auditor's report (per CAM)
  - footnote        — notes to the financial statements (per numbered note)
  - mda             — Management's Discussion and Analysis (Item 7)

Why these: the auditor's report + CAMs are the only place audit *opinions* surface
in public filings; the notes are the richest applied-GAAP prose; MD&A is the
narrative. Together they are the corpus's applied-accounting backbone.

Sources (all public, free):
  - Ticker->CIK map:  https://www.sec.gov/files/company_tickers.json
  - Submissions API:  https://data.sec.gov/submissions/CIK##########.json
  - Filing document:  https://www.sec.gov/Archives/edgar/data/<cik>/<accession>/<doc>

Usage (one command):
    python -m src.collectors.sec_edgar --out data/corpus/sec_filings.jsonl

SEC rules: a descriptive User-Agent declaring identity is required, and requests
are capped at 10/sec. We cache every fetch and sleep between live hits, so a
re-run does no network I/O. Pass --refresh to re-download.
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
from bs4 import BeautifulSoup

# SEC requires a User-Agent that identifies the requester (declare identity).
USER_AGENT = "AuditLM research collector (vimalnak@gmail.com)"

TICKERS_URL = "https://www.sec.gov/files/company_tickers.json"
SUBMISSIONS_URL = "https://data.sec.gov/submissions/CIK{cik10}.json"
ARCHIVE_DOC_URL = "https://www.sec.gov/Archives/edgar/data/{cik}/{acc}/{doc}"

# A diverse, industry-spanning default sample to validate parsing across the
# many ways filers format their 10-Ks before scaling up.
DEFAULT_TICKERS = [
    # Technology / hardware / software
    "AAPL", "MSFT", "NVDA", "CSCO", "ORCL", "ADBE", "CRM", "INTC", "AMD",
    "QCOM", "TXN", "IBM", "INTU",
    # Internet / communications
    "GOOGL", "META", "AMZN", "NFLX", "CMCSA", "TMUS",
    # Retail / consumer discretionary
    "WMT", "COST", "HD", "TGT", "NKE", "LOW", "SBUX", "MCD", "TJX",
    # Health care / pharma
    "JNJ", "PFE", "UNH", "MRK", "ABBV", "LLY", "ABT", "TMO", "DHR", "BMY",
    "AMGN", "GILD", "CVS", "MDT",
    # Energy
    "XOM", "CVX", "COP", "SLB", "EOG", "PSX", "VLO", "OXY",
    # Financials
    "JPM", "BAC", "GS", "MS", "WFC", "C", "SCHW", "AXP", "BLK",
    # Industrials / manufacturing
    "CAT", "BA", "GE", "HON", "DE", "MMM", "LMT", "RTX", "UNP", "FDX",
    # Consumer staples
    "KO", "PG", "PEP", "MO", "CL", "KMB", "GIS", "MDLZ", "HSY",
    # Telecom / media
    "VZ", "T", "DIS",
    # Utilities
    "DUK", "SO", "NEE", "AEP",
    # Materials
    "LIN", "SHW", "FCX",
    # Real estate
    "AMT", "PLD",
    # Transport
    "UPS", "DAL",
]

# A line that begins a citable note, e.g. "Note 1 –" / "NOTE 12 —" / "Note 3:".
# Case-insensitive: filers split between title-case and all-caps note headings.
NOTE_START = re.compile(r"^Note\s+(\d+)\s*[–—:.\-]", re.I)


def normalize_ws(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def fetch(url: str, cache_path: Path, client: httpx.Client, sleep: float, refresh: bool) -> str:
    """Return response text, using the on-disk cache unless --refresh is set."""
    if cache_path.exists() and not refresh:
        return cache_path.read_text(encoding="utf-8")
    resp = client.get(url, timeout=60.0, follow_redirects=True)
    resp.raise_for_status()
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    cache_path.write_text(resp.text, encoding="utf-8")
    time.sleep(sleep)  # politeness delay only on a real network hit
    return resp.text


def load_ticker_map(client, cache_dir, sleep, refresh) -> dict[str, dict]:
    """Map upper-case ticker -> {cik, title}."""
    raw = fetch(TICKERS_URL, cache_dir / "company_tickers.json", client, sleep, refresh)
    data = json.loads(raw)
    return {
        row["ticker"].upper(): {"cik": int(row["cik_str"]), "title": row["title"]}
        for row in data.values()
    }


def latest_10k(submissions: dict) -> dict | None:
    """Return metadata for the company's most recent 10-K, or None."""
    recent = submissions["filings"]["recent"]
    for i, form in enumerate(recent["form"]):
        if form == "10-K":
            return {
                "accession": recent["accessionNumber"][i],
                "filing_date": recent["filingDate"][i],
                "primary_doc": recent["primaryDocument"][i],
            }
    return None


def html_to_lines(html: str) -> list[str]:
    """Flatten filing HTML to non-empty, whitespace-collapsed text lines."""
    soup = BeautifulSoup(html, "html.parser")
    text = soup.get_text("\n")
    text = re.sub(r"[ \t ]+", " ", text)
    return [ln.strip() for ln in text.split("\n") if ln.strip()]


def is_page_footer(line: str) -> bool:
    """Running page headers/footers like 'Apple Inc. | 2025 Form 10-K | 51'."""
    return bool(re.search(r"\|\s*\d{4}\s*Form\s*10-K\s*\|", line))


def is_addressee(lines: list[str], i: int) -> bool:
    """True if an auditor's-report addressee begins at line ``i``.

    Filers vary in word order — "To the Shareholders and the Board of Directors
    of X" (Apple) vs "To the Board of Directors and Shareholders of X" (NVIDIA) —
    in the holder term — "Shareowners" (Coca-Cola, UPS) — and in line breaks: the
    addressee often wraps, with "To the" on one line and "Board of Directors and
    Stockholders of X" on the next (Chevron), or the company name alone wrapping
    (UPS). We require the line to open with "To the", then test the components
    against that line joined with the next so a wrapped addressee still matches.
    """
    low = lines[i].lower().strip()
    combined = low if i + 1 >= len(lines) else low + " " + lines[i + 1].lower().strip()
    has_parts = ("board of directors" in combined
                 and ("shareholders" in combined or "stockholders" in combined
                      or "shareowners" in combined))
    if not has_parts:
        return False
    if low.startswith("to the"):
        return True
    # Some firms (EY for Amazon, Lockheed) drop the "To the" and open the
    # addressee with "[The] Board of Directors and Shareholders". Accept that only
    # when the report's title sits just above, so a stray sentence mentioning the
    # board and shareholders is not mistaken for the addressee.
    if low.startswith(("the board of directors", "board of directors")):
        prev = " ".join(lines[max(0, i - 3):i]).lower()
        return "report of independent registered" in prev
    return False


# The auditor's signature closes every PCAOB report: a "/s/ Firm" line and the
# AS 3101-required tenure line ("We have served as the Company's auditor since
# YYYY"). Unlike the report title or "Item"/"Part" headings, these never recur as
# running page headers, so they give a clean, filer-independent report boundary.
SERVED_SINCE = re.compile(r"served as the .{0,40}auditor since", re.I)


def is_signature(line: str) -> bool:
    """True for an auditor's-report signature line ('/s/ ...' or the tenure line)."""
    return line.strip().startswith("/s/") or bool(SERVED_SINCE.search(line))


# An accounting-firm signature, used to end the report block at the auditor's
# sign-off rather than the 10-K's own signature page (which is signed by a
# corporate officer — "/s/ Jane Doe, Vice President" — not a firm).
FIRM = re.compile(r"\bL\.?L\.?P\.?\b|PricewaterhouseCoopers|Ernst\s*&\s*Young"
                  r"|KPMG|Deloitte", re.I)


def is_auditor_signoff(lines: list[str], i: int) -> bool:
    """True if line ``i`` is an auditor's sign-off (tenure line or '/s/ <Firm>')."""
    if SERVED_SINCE.search(lines[i]):
        return True
    if lines[i].strip().startswith("/s/"):
        return bool(FIRM.search(" ".join(lines[i:i + 3])))
    return False


def find_body_anchor(lines: list[str], pattern: str) -> int | None:
    """Find the anchor occurrence followed by the most body text.

    A heading that appears in the table of contents is followed by a short title
    and a page number; the real section is followed by substantial prose. Picking
    the occurrence with the most following text reliably skips the TOC copy.
    """
    best, best_score = None, -1
    pat = re.compile(pattern, re.I)
    for i, ln in enumerate(lines):
        if pat.search(ln):
            body = sum(len(x) for x in lines[i + 1 : i + 8])
            if body > best_score:
                best, best_score = i, body
    return best


def find_mda_start(lines: list[str]) -> int | None:
    """Find the real "Item 7. Management's Discussion and Analysis" heading.

    Filers split between one-line headings ("Item 7. Management's Discussion...")
    and two-line headings ("ITEM 7." then "MANAGEMENT'S DISCUSSION..."), and
    title- vs all-caps. We accept any "Item 7" (not 7A) whose own line or next
    couple of lines mention management's discussion, then pick the occurrence
    with the most following prose to skip the table-of-contents copy.
    """
    cands = []
    for i, ln in enumerate(lines):
        if re.match(r"^item\s*7\b", ln, re.I) and not re.match(r"^item\s*7a\b", ln, re.I):
            window = " ".join(lines[i : i + 3]).lower()
            if "management" in window and "discussion" in window:
                cands.append(i)
    if not cands:
        return None
    return max(cands, key=lambda i: sum(len(x) for x in lines[i + 1 : i + 9]))


def _is_title_like(t: str) -> bool:
    """A note heading is a short noun phrase — not a sentence.

    Distinguishes a real heading ("Income Taxes") from a false numbered match in
    prose, whose "title" is a full sentence ending in a period and running many
    words (e.g. Morgan Stanley's stray "5." → "Losses related to Other Assets ...
    discontinued leased properties.").
    """
    return bool(t) and not t.rstrip().endswith(".") and len(t.split()) <= 16


def _next_title(lines: list[str], i: int, strict: bool) -> str | None:
    """Title carried on the line after a heading whose own line has no title.

    ``strict`` requires a heading-like line (starts with two letters, capped
    length, reads like a title) — used for forms that are ambiguous on their own
    ("Note 2", "8.") so a wrapped cross-reference ("...Note 2 / to the financial
    statements") or a stray prose match is not mistaken for a note title.
    """
    if i + 1 >= len(lines):
        return None
    nxt = lines[i + 1].strip()
    if strict:
        ok = re.fullmatch(r"[A-Z][A-Za-z].{1,120}", nxt) and _is_title_like(nxt)
        return nxt if ok else None
    if nxt and not is_page_footer(nxt) and not re.fullmatch(r"\d+", nxt):
        return nxt
    return None


def _note_runs(cands: list[tuple[int, int, str]]) -> list[list[tuple[int, int, str]]]:
    """Break candidates into runs of consecutive note numbers 1, 2, 3, ...

    A run restarts at every "1" and ends at the first gap. This is deliberately
    bridge-free: a numbered list elsewhere in the filing (Pfizer's "1. Maximize
    ... 2. Deliver ..." strategic priorities, McDonald's core values) forms its
    own short run that the caller rejects on spacing, and cannot fuse onto the
    real notes the way a gap-tolerant matcher would (which makes one note a
    document-sized blob).
    """
    runs, cur = [], []
    for i, n, t in cands:
        if n == 1:
            if cur:
                runs.append(cur)
            cur = [(i, n, t)]
        elif cur and n == cur[-1][1] + 1:
            cur.append((i, n, t))
    if cur:
        runs.append(cur)
    return runs


def _collect_note_cands(lines, lo, hi):
    """Candidate (line, number, title) note headings between lo and hi, in every
    flattened-HTML form, split into numeric and letter ("Note A.") pools."""
    numeric: list[tuple[int, int, str]] = []
    letter: list[tuple[int, int, str]] = []
    for i in range(lo, hi):
        ln = lines[i]
        m = NOTE_START.match(ln)
        if m:  # "Note 1 – Title" (title may wrap to the next line)
            title = NOTE_START.sub("", ln).strip() or _next_title(lines, i, strict=False)
            if title:
                numeric.append((i, int(m.group(1)), title))
            continue
        lm = re.fullmatch(r"note\s+([A-Za-z])\.?", ln.strip(), re.I)  # letter note "Note A."
        if lm:
            title = _next_title(lines, i, strict=True)
            if title:
                letter.append((i, ord(lm.group(1).upper()) - ord("A") + 1, title))
            continue
        bare = re.fullmatch(r"note\s+(\d+)", ln.strip(), re.I)
        if bare:  # bare "Note 1" (number alone), title on the next line ...
            if _next_title(lines, i, strict=True):
                numeric.append((i, int(bare.group(1)), _next_title(lines, i, strict=True)))
            # ... or "Note 3 / . / Title" with a lone period between (Kimberly-Clark)
            elif i + 1 < len(lines) and lines[i + 1].strip() == "." and _next_title(lines, i + 1, strict=True):
                numeric.append((i, int(bare.group(1)), _next_title(lines, i + 1, strict=True)))
            continue
        m = re.match(r"^(\d+)\.\s+([A-Z][A-Za-z].{1,120})$", ln)
        if m and _is_title_like(m.group(2).strip()):  # "1. Title" inline
            numeric.append((i, int(m.group(1)), m.group(2).strip()))
            continue
        dot = re.match(r"^(\d+)\.$", ln)
        if dot:  # "1." with the title on the next line
            title = _next_title(lines, i, strict=True)
            if title:
                numeric.append((i, int(dot.group(1)), title))
            continue
        # Number, a lone ".", and the title each on their own line (Target).
        if re.fullmatch(r"\d+", ln) and i + 1 < len(lines) and lines[i + 1].strip() == ".":
            title = _next_title(lines, i + 1, strict=True)
            if title:
                numeric.append((i, int(ln), title))
    return numeric, letter


def find_notes(lines: list[str], end: int) -> list[tuple[int, str | None, str | None]]:
    """Locate notes, returning (line_index, note_number, title).

    Filers label notes many ways once HTML is flattened to lines — "Note 1 –
    Title", bare "Note 1"/"NOTE 1" with the title on the next line, "Note 1." with
    a lone period and the title below (Kimberly-Clark), "1. Title", "1." then the
    title, a number/"."/title split across three lines, and a letter ("Note A.",
    TJX). We gather candidates in all of these forms, group them into runs 1, 2,
    3, ..., and keep the run that spans the most document lines.

    The span test separates the real notes from a notes *index*: a filing lists
    its notes twice — a compact index (headings within ~80 lines, no bodies) and
    the real notes (spread across thousands of lines, with prose). Both are clean
    1..N runs, so counting can't tell them apart, but the real notes span far more
    of the document.
    """
    numeric, letter = _collect_note_cands(lines, 0, min(end, len(lines)))
    runs = _note_runs(numeric) + _note_runs(letter)

    # Reject the Part IV "Item 15" listing ("1. Financial Statements / 2. Financial
    # Statement Schedules / 3. Exhibits"). Match by prefix, not exact title: some
    # filers spell the entries out (IBM). "Financial statement(s)" as a prefix is
    # safe — real notes are "Financial Instruments", never "Financial Statement".
    def _is_part_iv(t: str) -> bool:
        tl = t.lower().strip()
        return (tl.startswith("financial statement")
                or tl.startswith("exhibit") or tl == "signatures")

    # Real notes carry substantial bodies, so headings sit far apart (observed
    # >=85 lines/note); a numbered *list* (a notes index, or McDonald's "1. Serve
    # / 2. Inclusion" core values) packs items every 2-3 lines.
    def _ok(chain) -> bool:
        return (len(chain) >= 3
                and not any(_is_part_iv(t) for _, _, t in chain)
                and (chain[-1][0] - chain[0][0]) / (len(chain) - 1) >= 10)
    runs = [r for r in runs if _ok(r)]
    if not runs:
        return []
    best = max(runs, key=lambda r: (r[-1][0] - r[0][0], len(r)))  # widest span wins
    return [(i, str(n), normalize_ws(t)) for i, n, t in best]


def slice_until(lines, start, end_patterns, max_lines=4000) -> tuple[list[str], int]:
    """Collect lines from start until a line matches any end pattern."""
    ends = [re.compile(p, re.I) for p in end_patterns]
    out = []
    i = start
    for i in range(start, min(start + max_lines, len(lines))):
        if i > start and any(p.search(lines[i]) for p in ends):
            break
        if not is_page_footer(lines[i]):
            out.append(lines[i])
    return out, i


def extract_sections(lines: list[str]) -> dict:
    """Extract the four high-value sections. Missing sections are omitted."""
    sections: dict = {}

    # --- MD&A: Item 7 -> Item 7A / Item 8 ---
    m = find_mda_start(lines)
    if m is not None:
        mda, _ = slice_until(lines, m, [r"^item\s*7a\b", r"^item\s*8\b"])
        if len(mda) > 5:
            sections["mda"] = [{"title": None, "text": "\n".join(mda)}]

    # --- Auditor's report -> next Item / Exhibit / Signatures ---
    # Anchor on the FIRST addressee line ("To the Shareholders/Stockholders and
    # the Board of Directors of X"). The report title itself is unreliable: some
    # filers break it mid-word across HTML text nodes ("...REGIST" / "ERED...")
    # so it never matches on a single flattened line, and the intact copies that
    # do match are table-of-contents/exhibit references. The addressee line is
    # present and intact. We take the first occurrence because a filing may
    # contain two reports (financial statements, then internal control) and the
    # critical audit matters are always in the first; the addressee line never
    # appears in the table of contents, so the first occurrence is the report.
    report_idx = next((i for i in range(len(lines)) if is_addressee(lines, i)), None)
    report_end = None
    if report_idx is not None:
        # Bound the report block at the auditor's sign-off rather than the next
        # "Item"/"Part" heading: those headings recur as running page headers
        # inside the report (e.g. Microsoft repeats "PART II"/"Item 8"), which
        # truncates the report — and any CAMs in it — far too early. A filing may
        # carry two reports (financial statements, then internal control); the last
        # auditor sign-off within the window closes the pair. We match the firm's
        # sign-off specifically, not any "/s/", so a report near the end of the
        # filing does not run on to the 10-K's own (officer-signed) signature page.
        # Fall back to the heading scan only when no sign-off is found.
        sig_idxs = [i for i in range(report_idx, min(report_idx + 600, len(lines)))
                    if is_auditor_signoff(lines, i)]
        if sig_idxs:
            report_end = min(sig_idxs[-1] + 4, len(lines))
            rep = [ln for ln in lines[report_idx:report_end] if not is_page_footer(ln)]
        else:
            rep, report_end = slice_until(
                lines, report_idx,
                [r"^item\s*\d", r"^exhibit\b", r"^signatures?\b", r"^part\s+i",
                 NOTE_START.pattern],
            )
        if len(rep) > 3:
            sections["auditor_report"] = [{"title": None, "text": "\n".join(rep)}]

        # --- CAMs: within the report, split per CAM ---
        cam_start = next(
            (i for i in range(report_idx, report_end)
             if re.search(r"critical audit matter", lines[i], re.I)),
            None,
        )
        if cam_start is not None:
            # CAMs belong to the financial-statement report only. Bound them at
            # the first auditor signature, or the addressee of a following report
            # (e.g. the separate internal-control opinion), so the CAM record does
            # not swallow that report. The signature ("/s/" or the tenure line) is
            # immune to the running page headers that would otherwise cut a multi-
            # CAM section short.
            cam_end = report_end
            for i in range(cam_start + 1, report_end):
                if is_signature(lines[i]) or is_addressee(lines, i):
                    cam_end = i
                    break
            cam_lines = [ln for ln in lines[cam_start:cam_end] if not is_page_footer(ln)]
            sections["critical_audit_matter"] = split_cams(cam_lines)

    # --- Footnotes: first numbered note -> auditor's report (per note) ---
    notes_end = report_idx if (report_idx is not None) else len(lines)
    marks = find_notes(lines, len(lines))
    if marks:
        first = marks[0][0]
        # Notes run until the auditor's report if it follows them, else to end.
        if report_idx is not None and report_idx > first:
            # Report AFTER the notes: back up from the addressee to the report's
            # title line so the last note doesn't end on "Report of Independent
            # Registered Public Accounting Firm".
            for j in range(report_idx - 1, max(report_idx - 6, first), -1):
                if re.match(r"report of independent registered", lines[j], re.I):
                    notes_end = j
                    break
        else:
            notes_end = len(lines)
            # Bank-style filings place the report *before* the notes, so the notes
            # run to EOF — and the last note would swallow whatever follows. Stop at
            # the first boundary the notes cannot cross: a later auditor's report
            # (the separate internal-control opinion) or the document tail (Part
            # III/IV, signatures, exhibits).
            tail = re.compile(r"^(signatures?|part\s+(iii|iv)\b|exhibit\s+index"
                              r"|item\s*(9|1[0-5])\b|report of independent registered)", re.I)
            for j in range(marks[-1][0] + 1, len(lines)):
                if tail.match(lines[j]) or is_addressee(lines, j):
                    notes_end = j
                    break
        sections["footnote"] = split_notes(lines, marks, notes_end)

    return sections


def split_cams(cam_lines: list[str]) -> list[dict]:
    """Split the CAM section into one record per critical audit matter.

    A CAM is named by a short title line, then describes the matter and how it
    was addressed. Filers signpost the description differently — "Description of
    the Matter" (e.g. EY) or "Critical Audit Matter Description" (e.g. Deloitte) —
    so we anchor each CAM on either phrasing and take its title as the preceding
    line.
    """
    desc_idxs = [i for i, ln in enumerate(cam_lines)
                 if re.match(r"(description of the matter"
                             r"|critical audit matter description)", ln, re.I)]
    if not desc_idxs:
        # PwC reports carry no labelled signpost: each CAM opens with "As
        # described in [Note N / the ...] financial statements", with the matter
        # title on the preceding line. Used only as a fallback, so it cannot
        # mis-split the labelled (EY/Deloitte) reports above.
        desc_idxs = [i for i, ln in enumerate(cam_lines)
                     if re.match(r"as described in\b", ln, re.I)]
    if not desc_idxs:
        # Couldn't segment individual CAMs; keep the whole section as one record.
        return [{"title": None, "text": "\n".join(cam_lines)}]

    cams = []
    for k, d in enumerate(desc_idxs):
        title = cam_lines[d - 1] if d > 0 else None
        end = desc_idxs[k + 1] - 1 if k + 1 < len(desc_idxs) else len(cam_lines)
        # CAM body starts at the title line (one line before the description).
        body_start = d - 1 if d > 0 else d
        text = "\n".join(cam_lines[body_start:end])
        cams.append({"title": normalize_ws(title) if title else None, "text": text})
    return cams


def split_notes(lines, marks, notes_end) -> list[dict]:
    """One record per numbered note. ``marks`` is (index, note_no, title).

    Skips notes-index entries: some filers number notes only in an index ("Note
    1: Basis of Presentation / 61 / Note 2: ... / 62") and head the real note
    bodies by name alone. An index entry is just a heading and a page number with
    no prose, so we keep a note only if its body has a real line of text. (Filers
    whose note bodies are name-only fall through with no footnotes rather than
    yielding index garbage — an honest gap, not a wrong record.)
    """
    records = []
    bounded = [(i, no, t) for (i, no, t) in marks if i < notes_end]
    for k, (start, note_no, title) in enumerate(bounded):
        # A running page header ("Goldman Sachs 2025 Form 10-K") occasionally lands
        # on a numbered line; "Form 10-K" in the title gives it away (never a real
        # note title) and would otherwise swallow the rest of the document.
        if title and re.search(r"form\s*10-K", title, re.I):
            continue
        end = bounded[k + 1][0] if k + 1 < len(bounded) else notes_end
        block = [ln for ln in lines[start:end] if not is_page_footer(ln)]
        # Body prose excludes the heading, the title line, and numeric/table rows.
        # A real note has a sentence of narrative here; a table-only note or a
        # stray cross-reference has only its title, so it is dropped (honest gap).
        body = [ln for ln in block[1:]
                if normalize_ws(ln) != title and not re.fullmatch(r"\d[\d,. ]*", ln)]
        if not any(len(ln) > 30 for ln in body):
            continue  # heading/title only -> notes index or table, not a note body
        records.append({"title": title, "note_no": note_no, "text": "\n".join(block)})
    return records


def collect_filing(info: dict, html: str, collected_at: str) -> list[dict]:
    """Parse one 10-K's HTML into per-section records."""
    lines = html_to_lines(html)
    sections = extract_sections(lines)
    records = []
    cik = info["cik"]
    for section, items in sections.items():
        for idx, item in enumerate(items):
            # Positional suffix guarantees unique ids; the citable note number is
            # preserved in metadata.
            suffix = f"{idx + 1}" if len(items) > 1 else ""
            rec_id = f"sec-{cik}-{info['filing_date']}-{section}"
            if suffix:
                rec_id += f"-{suffix}"
            meta = {"note_no": item["note_no"]} if item.get("note_no") else {}
            records.append({
                "id": rec_id.lower(),
                "source": "SEC",
                "doc_type": "10-K",
                "form_type": "10-K",
                "company": info["company"],
                "cik": cik,
                "sic": info.get("sic"),
                "sic_description": info.get("sic_description"),
                "filing_date": info["filing_date"],
                "section": section,
                "section_title": item.get("title"),
                "text": item["text"],
                "url": info["url"],
                "metadata": meta,
                "collected_at": collected_at,
            })
    return records


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Collect high-value sections from SEC 10-K filings.")
    ap.add_argument("--out", type=Path, default=Path("data/corpus/sec_filings.jsonl"))
    ap.add_argument("--cache", type=Path, default=Path("data/raw/sec"))
    ap.add_argument("--tickers", nargs="*", default=DEFAULT_TICKERS,
                    help="Tickers to collect (default: a diverse industry sample).")
    ap.add_argument("--limit", type=int, default=None, help="Only the first N tickers.")
    ap.add_argument("--sleep", type=float, default=0.3,
                    help="Seconds between network fetches (SEC limit is 10/sec).")
    ap.add_argument("--refresh", action="store_true", help="Ignore cache, re-download.")
    args = ap.parse_args(argv)

    collected_at = datetime.now(timezone.utc).isoformat()
    headers = {"User-Agent": USER_AGENT}
    tickers = args.tickers[: args.limit] if args.limit else args.tickers

    all_records: list[dict] = []
    skipped: list[str] = []

    with httpx.Client(headers=headers) as client:
        ticker_map = load_ticker_map(client, args.cache, args.sleep, args.refresh)

        for ticker in tickers:
            t = ticker.upper()
            entry = ticker_map.get(t)
            if not entry:
                skipped.append(f"{t} (unknown ticker)")
                print(f"[warn] unknown ticker {t}", file=sys.stderr)
                continue
            cik = entry["cik"]
            cik10 = f"{cik:010d}"
            sub_raw = fetch(SUBMISSIONS_URL.format(cik10=cik10),
                            args.cache / f"sub_{cik10}.json", client, args.sleep, args.refresh)
            submissions = json.loads(sub_raw)
            filing = latest_10k(submissions)
            if not filing:
                skipped.append(f"{t} (no 10-K)")
                print(f"[warn] no 10-K for {t}", file=sys.stderr)
                continue

            acc_nodash = filing["accession"].replace("-", "")
            url = ARCHIVE_DOC_URL.format(cik=cik, acc=acc_nodash, doc=filing["primary_doc"])
            html = fetch(url, args.cache / f"{cik10}_10k.htm", client, args.sleep, args.refresh)

            info = {
                "cik": cik,
                "company": submissions.get("name") or entry["title"],
                "sic": submissions.get("sic"),
                "sic_description": submissions.get("sicDescription"),
                "filing_date": filing["filing_date"],
                "url": url,
            }
            recs = collect_filing(info, html, collected_at)
            if not recs:
                skipped.append(f"{t} (no sections extracted)")
                print(f"[warn] no sections extracted for {t}", file=sys.stderr)
                continue
            all_records.extend(recs)
            counts = {}
            for r in recs:
                counts[r["section"]] = counts.get(r["section"], 0) + 1
            print(f"[ok] {t:6} {info['company'][:34]:34} {counts}", file=sys.stderr)

    args.out.parent.mkdir(parents=True, exist_ok=True)
    with args.out.open("w", encoding="utf-8") as f:
        for r in all_records:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")

    n_filings = len({(r["cik"], r["filing_date"]) for r in all_records})
    print(f"\n[done] {len(all_records)} records from {n_filings} filings -> {args.out}",
          file=sys.stderr)
    if skipped:
        print(f"[done] skipped: {', '.join(skipped)}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
