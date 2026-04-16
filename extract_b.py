"""
Source B Extractor — Polish Political Leaders Directory
Target URLs: https://rulers.org/polgov.html, https://rulers.org/polvoi2.html, https://rulers.org/rulp2.html

Architecture:
  Declarative, specification-driven extraction using Pydantic models as the
  single source of truth.  Two backend strategies are employed transparently:

    1. Structured regex parser  — fast, zero-quota path for well-formatted
       date-range entries (governors, presidents, prime ministers).
    2. LLM semantic extractor   — Groq/Gemini Turbo-V2 for ambiguous entries
       that the regex parser cannot handle (cabinet ministers, edge cases).

  Both backends produce the same PEPPerson Pydantic model.  The orchestrator
  is indifferent to which backend produced the record.

  Resume-on-failure is supported via MD5 content hashing of each raw entry.
"""

import re
import json
import sys
import html
import hashlib
from datetime import date, timedelta
from typing import Optional
from pathlib import Path

import requests
from bs4 import BeautifulSoup

sys.path.insert(0, ".")
from models import SourceBOutput, PEPPerson, PEPMetadata

# ─── Conditional LLM import ──────────────────────────────────────────────────
try:
    from llm_extraction.llm_extractor import extract_source_b_entry, load_api_keys
    LLM_AVAILABLE = True
except ImportError:
    LLM_AVAILABLE = False

SOURCE_URLS = [
    "https://rulers.org/polgov.html",   # Poland government ministers
    "https://rulers.org/polvoi2.html",  # Polish voivodeship governors
    "https://rulers.org/rulp2.html",    # Poland presidents and prime ministers
]

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.5",
    "Accept-Encoding": "gzip, deflate",
}

PARTY_CODES = {
    "AWS", "BBWR", "ChD", "ChZJN", "LPR", "ND", "NL", "NZL", "OKP",
    "OZN", "PChD", "PiS", "PL2050", "PO", "PPChD", "PPN", "PPR",
    "PSL", "PSLW", "PZKS", "PZPR", "SD", "SdPl", "SdRP", "SKL",
    "SN", "SND", "SO", "SP", "UP", "UW"
}

# Cutoff: last 12 months from today
TODAY = date.today()
CUTOFF_DATE = TODAY - timedelta(days=365)

# ─── Date parsing ────────────────────────────────────────────────────────────

MONTH_MAP = {
    "jan": "01", "feb": "02", "mar": "03", "apr": "04",
    "may": "05", "jun": "06", "jul": "07", "aug": "08",
    "sep": "09", "oct": "10", "nov": "11", "dec": "12"
}


def parse_rulers_date(raw: str) -> Optional[str]:
    """
    Parse a date string from rulers.org into ISO format.
    rulers.org uses formats like:
      "1 Jan 2024", "Jan 2024", "2024", "15 Mar 2023", "Dec 2022"
    Returns partial dates (YYYY or YYYY-MM) when full date unavailable.
    Returns None for unparseable values.
    """
    if not raw:
        return None
    raw = raw.strip().strip(".")

    if re.match(r"^(present|current|now|–\s*$)", raw, re.IGNORECASE):
        return None

    m = re.match(r"^(\d{1,2})\s+([A-Za-z]{3})\s+(\d{4})$", raw)
    if m:
        mon = MONTH_MAP.get(m.group(2).lower()[:3])
        if mon:
            return f"{m.group(3)}-{mon}-{m.group(1).zfill(2)}"

    m = re.match(r"^([A-Za-z]{3})\s+(\d{4})$", raw)
    if m:
        mon = MONTH_MAP.get(m.group(1).lower()[:3])
        if mon:
            return f"{m.group(2)}-{mon}"

    m = re.match(r"^(\d{4})$", raw)
    if m:
        return m.group(1)

    if re.match(r"^\d{4}-\d{2}(-\d{2})?$", raw):
        return raw

    return None


def date_from_partial(partial: Optional[str]) -> Optional[date]:
    """
    Convert a partial ISO date string to a date object for comparison.
    YYYY -> first day of year; YYYY-MM -> first day of month.
    """
    if not partial:
        return None
    parts = partial.split("-")
    try:
        if len(parts) >= 3:
            return date(int(parts[0]), int(parts[1]), int(parts[2]))
        elif len(parts) == 2:
            return date(int(parts[0]), int(parts[1]), 1)
        else:
            return date(int(parts[0]), 1, 1)
    except (ValueError, IndexError):
        return None


def is_within_last_12_months(start: Optional[str], end: Optional[str]) -> bool:
    """
    Return True if the person was serving at any point in the last 12 months.
      - If end is None or unparseable (still serving): include
      - If end is set and parseable: must have ended after the cutoff date
    """
    end_date_obj = date_from_partial(end) if end else TODAY
    if end_date_obj is None:
        end_date_obj = TODAY  # assume still serving if unparseable
    return end_date_obj >= CUTOFF_DATE


# ─── Section role mapping ────────────────────────────────────────────────────

SECTION_ROLE_MAP = {
    "president": ("Head of State", "President"),
    "head of state": ("Head of State", "Head of State"),
    "prime minister": ("Prime Minister", "Prime Minister"),
    "premier": ("Prime Minister", "Prime Minister"),
    "minister": ("Minister", None),
    "cabinet": ("Minister", None),
    "secretary": ("Minister", None),
    "governor": ("Governor", "Governor"),
    "voivod": ("Governor", "Voivode"),
    "marshal": ("Governor", "Marshal of the Sejm"),
    "speaker": ("Senior Official", "Speaker"),
    "chairman": ("Senior Official", None),
}


def classify_role(section_header: str, entry_text: str) -> tuple[str, Optional[str]]:
    """Determine (role, role_detail) from section header and entry text."""
    header_lower = section_header.lower()
    for key, (role, detail) in SECTION_ROLE_MAP.items():
        if key in header_lower:
            if detail is None:
                detail = extract_role_detail(entry_text, section_header)
            return role, detail
    return "Other", section_header.strip(": \n")


def extract_role_detail(entry_text: str, section_header: str) -> Optional[str]:
    """For minister entries, attempt to identify the specific portfolio."""
    m = re.search(r"(Minister\s+of\s+[A-Za-z\s\-,]+)", entry_text, re.IGNORECASE)
    if m:
        return m.group(1).strip()
    clean = re.sub(r"[:\-]+", "", section_header).strip()
    return clean if clean else None


# ─── Name cleaning ───────────────────────────────────────────────────────────

def clean_name(raw: str) -> str:
    """
    Clean a name string:
    - Strip trailing/leading punctuation
    - Preserve all Unicode characters (diacritics mandatory)
    - Collapse repeated whitespace
    """
    name = raw.strip().strip(".,;:-")
    name = re.sub(r"\s+", " ", name)
    name = re.sub(r"^\d+\.\s*", "", name)
    return name.strip()


# ─── Regex-based entry parser (fast, zero-quota) ─────────────────────────────

def parse_entry_line(line: str, section_header: str, default_role: Optional[str] = None) -> Optional[PEPPerson]:
    """
    Parse a single person entry line from rulers.org using regex.
    This is the fast, zero-API-cost backend.  It handles the common
    "date - date  Name  PARTY" format used across rulers.org.
    """
    line = line.strip()
    if not line or len(line) < 10:
        return None

    if re.match(r"^[A-Z\s]{4,}[:\-]?\s*$", line):
        return None
    if line.startswith("(") and line.endswith(")"):
        return None

    birth_year = None
    birth_match = re.search(r"\(b\.?\s*(\d{4})\)", line, re.IGNORECASE)
    if birth_match:
        birth_year = birth_match.group(1)
        line = line[:birth_match.start()].rstrip() + line[birth_match.end():]

    notes_parts: list[str] = []
    for note_pat in [r"\((acting)\)", r"\((\d+(?:st|nd|rd|th) time)\)", r"\((f)\)", r"\((s\.a\.)\)"]:
        for match in re.findall(note_pat, line, re.IGNORECASE):
            notes_parts.append(match.strip())
        line = re.sub(note_pat, "", line, flags=re.IGNORECASE).strip()

    start_raw = None
    end_raw = None
    remainder = None

    date_pattern = r"(?:\d{1,2}\s+[A-Za-z]{3}\s+\d{4}|[A-Za-z]{3}\s+\d{4}|\d{4})"
    prefix_match = re.match(
        rf"^({date_pattern})\s*[-\u2013]\s*({date_pattern}|present|current|now)?\s*(.*)$",
        line,
        re.IGNORECASE,
    )
    if prefix_match:
        start_raw = prefix_match.group(1).strip()
        end_raw = prefix_match.group(2).strip() if prefix_match.group(2) else None
        remainder = prefix_match.group(3).strip()
    else:
        suffix_match = re.match(
            rf"^(.+?)\s*,?\s*({date_pattern})\s*[-\u2013]\s*({date_pattern}|present|current|now)?\s*$",
            line,
            re.IGNORECASE,
        )
        if suffix_match:
            remainder = suffix_match.group(1).strip()
            start_raw = suffix_match.group(2).strip()
            end_raw = suffix_match.group(3).strip() if suffix_match.group(3) else None

    if not start_raw:
        return None

    if end_raw and re.match(r"^(present|current|now|\u2013+\s*$|\s*)$", end_raw, re.IGNORECASE):
        end_raw = None

    if remainder:
        remainder = re.sub(r"\s+[-\u2013]+\s*$", "", remainder).strip()
        remainder = re.sub(r"\s*\([^)]*\)", "", remainder).strip()
    else:
        remainder = ""

    party_match = re.search(r"\b([A-Z][A-Z0-9]{0,5}(?:/[A-Z][A-Z0-9]{0,5})?)\s*$", remainder)
    if party_match:
        candidate = party_match.group(1)
        if candidate.upper() in PARTY_CODES:
            notes_parts.append(candidate)
            remainder = remainder[:party_match.start()].strip()

    name = clean_name(remainder)
    if not name or len(name) < 3:
        return None

    start_date = parse_rulers_date(start_raw)
    end_date = parse_rulers_date(end_raw)
    currently_serving = end_date is None

    if not currently_serving and end_date:
        end_obj = date_from_partial(end_date)
        if end_obj and end_obj < date(1990, 1, 1):
            return None

    if not is_within_last_12_months(start_date, end_date):
        return None

    role, role_detail = classify_role(section_header, line)
    if role == "Other" and default_role:
        role = default_role
        role_detail = section_header.strip() or role_detail

    return PEPPerson(
        name=name,
        role=role,
        role_detail=role_detail,
        start_date=start_date,
        end_date=end_date,
        currently_serving=currently_serving,
        birth_year=birth_year,
        notes="; ".join(notes_parts) if notes_parts else None,
    )


# ─── Page parsers (regex backend) ────────────────────────────────────────────

def normalize_wrapped_lines(lines: list[str]) -> list[str]:
    """Preserve each line as its own entry while removing empty lines."""
    return [line for line in (line.rstrip() for line in lines) if line.strip()]


def parse_polgov_page(soup: BeautifulSoup) -> list[PEPPerson]:
    """Parse polgov.html (Ministers) via regex backend."""
    persons: list[PEPPerson] = []
    current_section = "Other"

    for tag in soup.find_all(['h2', 'h3', 'pre']):
        if tag.name in ('h2', 'h3'):
            current_section = tag.get_text().strip()
            continue
        if tag.name != 'pre':
            continue

        lines = normalize_wrapped_lines(tag.get_text(separator="\n").split("\n"))
        for line in lines:
            stripped = line.strip()
            if not stripped:
                continue
            if re.match(r'^(Ministers|Deputy prime ministers|Directors|Governors|Marshals|Speakers|Heads of state|Presidents|Prime ministers|Ministries|Departments|\-.*\-)$', stripped, re.IGNORECASE):
                continue
            person = parse_entry_line(stripped, current_section, default_role="Minister")
            if person:
                persons.append(person)

    return persons


def parse_polvoi2_page(soup: BeautifulSoup) -> list[PEPPerson]:
    """Parse polvoi2.html (Governors) via regex backend."""
    persons: list[PEPPerson] = []
    for heading in soup.find_all('h2'):
        region = heading.get_text().strip()
        pre = heading.find_next_sibling('pre')
        if pre is None:
            continue
        lines = normalize_wrapped_lines(pre.get_text(separator="\n").split("\n"))
        for line in lines:
            stripped = line.strip()
            if not stripped or re.match(r'^(Governors|Note:|województwo|wojewodowie)$', stripped, re.IGNORECASE):
                continue
            person = parse_entry_line(stripped, region, default_role="Governor")
            if person:
                persons.append(person)

    return persons


def parse_rulp2_presidents_and_pms(html_content: str) -> list[PEPPerson]:
    """
    Extract Presidents and Prime Ministers from rulp2.html using anchor-based
    Poland block extraction.
    """
    html_lower = html_content.lower()
    start_idx = html_lower.find('<a name="poland"')
    if start_idx == -1:
        return []

    end_idx = html_lower.find("<a name=", start_idx + 20)
    if end_idx == -1:
        end_idx = len(html_content)

    poland_block = html_content[start_idx:end_idx]

    # Split on Prime ministers marker
    presidents_html = poland_block.split("<B>Prime ministers</B>")[0] if "<B>Prime ministers</B>" in poland_block else ""
    pm_html = poland_block[poland_block.find("<B>Prime ministers</B>"):] if "<B>Prime ministers</B>" in poland_block else ""

    persons: list[PEPPerson] = []
    if presidents_html:
        persons.extend(_parse_rulp2_block(presidents_html, "Head of State", "President"))
    if pm_html:
        persons.extend(_parse_rulp2_block(pm_html, "Prime Minister", "Prime Minister"))

    return persons


def _parse_rulp2_block(html_block: str, default_role: str, default_role_detail: str) -> list[PEPPerson]:
    """Parse a block from rulp2.html (Presidents or Prime Ministers)."""
    as_text = re.sub(r"<br\s*/?>", "\n", html_block, flags=re.IGNORECASE)
    as_text = re.sub(r"</(p|div|li|tr|pre|h[1-6])>", "\n", as_text, flags=re.IGNORECASE)
    as_text = re.sub(r"<[^>]+>", "", as_text)
    as_text = html.unescape(as_text)

    lines = [re.sub(r"\s+", " ", line).strip() for line in as_text.splitlines()]
    lines = [line for line in lines if line and len(line) > 5]

    persons: list[PEPPerson] = []

    date_pattern = r"(?:\d{1,2}\s+[A-Za-z]{3}\s+\d{4}|[A-Za-z]{3}\s+\d{4}|\d{4})"
    entry_regex = rf"^({date_pattern})\s*[-\u2013]\s*({date_pattern}|present|current|now)?\s*(.+)$"

    for line in lines:
        match = re.match(entry_regex, line, re.IGNORECASE)
        if not match:
            continue

        start_raw = match.group(1)
        end_raw = match.group(2) or ""
        remainder = match.group(3).strip()

        if not remainder or len(remainder) < 3:
            continue
        if re.match(r"^(Note:|See also:|In exile|Acting|Regent)", remainder, re.IGNORECASE):
            continue

        birth_match = re.search(r"\(b\.\s*(\d{4})", remainder, re.IGNORECASE)
        birth_year = birth_match.group(1) if birth_match else None

        remainder = re.sub(r"\(b\..+?\)", "", remainder, flags=re.IGNORECASE).strip()
        remainder = re.sub(r"\(d\..+?\)", "", remainder, flags=re.IGNORECASE).strip()
        remainder = re.sub(r"\(s\.a\.\)", "", remainder, flags=re.IGNORECASE).strip()
        remainder = re.sub(r"\([^)]*acting[^)]*\)", "", remainder, flags=re.IGNORECASE).strip()

        name = clean_name(remainder)
        if not name or len(name) < 3:
            continue

        start_date = parse_rulers_date(start_raw)
        end_date = None
        currently_serving = False

        if end_raw and not re.match(r"^(present|current|now)$", end_raw, re.IGNORECASE):
            end_date = parse_rulers_date(end_raw)
        else:
            currently_serving = True

        if not currently_serving and end_date:
            end_obj = date_from_partial(end_date)
            if end_obj and end_obj < date(1990, 1, 1):
                continue

        if currently_serving and start_date:
            start_obj = date_from_partial(start_date)
            if start_obj and start_obj < date(1990, 1, 1):
                continue

        if not is_within_last_12_months(start_date, end_date):
            continue

        persons.append(
            PEPPerson(
                name=name,
                role=default_role,
                role_detail=default_role_detail,
                start_date=start_date,
                end_date=end_date,
                currently_serving=currently_serving,
                birth_year=birth_year,
                notes=None,
            )
        )

    return persons


# ─── Checkpointing utilities ─────────────────────────────────────────────────

def get_entry_hash(text: str, section: str) -> str:
    """Generate a stable MD5 fingerprint for a Source B entry."""
    data = f"{section}:{text.strip()}"
    return hashlib.md5(data.encode("utf-8")).hexdigest()


# ─── LLM candidate collection (for entries regex cannot parse) ────────────────

def collect_llm_candidates(html_content: str, page_label: str) -> list[tuple[str, str]]:
    """
    For polgov.html: collect raw entry lines that the regex parser could NOT
    handle.  These will be sent to the LLM for semantic extraction.
    """
    soup = BeautifulSoup(html_content, "lxml")
    candidates: list[tuple[str, str]] = []
    current_section = "Other"

    for tag in soup.find_all(['h2', 'h3', 'pre']):
        if tag.name in ('h2', 'h3'):
            current_section = tag.get_text().strip()
            continue
        if tag.name != 'pre':
            continue

        lines = normalize_wrapped_lines(tag.get_text(separator="\n").split("\n"))
        for line in lines:
            stripped = line.strip()
            if not stripped or len(stripped) < 15:
                continue
            if re.match(r'^(Ministers|Deputy|Directors|Governors|Marshals|Speakers|Heads|Presidents|Prime|Ministries|Departments|\-.*\-)$', stripped, re.IGNORECASE):
                continue

            # Try regex first — if it fails, this is an LLM candidate
            person = parse_entry_line(stripped, current_section, default_role="Minister")
            if person is None:
                # Only if it looks like it could be a person entry
                has_year = any(y in stripped for y in ["2024", "2025", "2026"])
                has_active = any(m in stripped.lower() for m in ["present", "current"])
                has_trailing_dash = bool(re.search(r"-\s*(\Z|\n)", stripped))
                if has_year or has_active or has_trailing_dash:
                    candidates.append((stripped, current_section))

    return candidates


# ─── Main orchestrator ────────────────────────────────────────────────────────

def extract_source_b(html_pages: list[tuple[str, str]]) -> SourceBOutput:
    """
    Main orchestrator for Source B with declarative dual-backend extraction.

    Strategy:
      1. Run regex parsers on all three pages (fast, zero quota cost)
      2. Collect entries the regex couldn't parse from polgov.html
      3. Send only those ambiguous entries to the LLM (Turbo-V2)
      4. Merge, deduplicate, validate through Pydantic
    """
    polgov_html = next((h for u, h in html_pages if "polgov.html" in u), "")
    polvoi2_html = next((h for u, h in html_pages if "polvoi2.html" in u), "")
    rulp2_html = next((h for u, h in html_pages if "rulp2.html" in u), "")

    persons: list[PEPPerson] = []

    # ── Phase 1: Regex backend (fast, zero-quota) ──
    print("\n  [Phase 1] Regex backend — structured entries...")

    # Presidents & Prime Ministers from rulp2.html
    if rulp2_html:
        pres_pm = parse_rulp2_presidents_and_pms(rulp2_html)
        print(f"    rulp2.html: {len(pres_pm)} presidents/PMs")
        persons.extend(pres_pm)

    # Governors from polvoi2.html
    if polvoi2_html:
        soup = BeautifulSoup(polvoi2_html, "lxml")
        govs = parse_polvoi2_page(soup)
        print(f"    polvoi2.html: {len(govs)} governors")
        persons.extend(govs)

    # Ministers from polgov.html (regex pass)
    regex_ministers: list[PEPPerson] = []
    if polgov_html:
        soup = BeautifulSoup(polgov_html, "lxml")
        regex_ministers = parse_polgov_page(soup)
        print(f"    polgov.html: {len(regex_ministers)} ministers (regex)")
        persons.extend(regex_ministers)

    print(f"  [Phase 1] Total from regex: {len(persons)} persons")

    # ── Phase 2: LLM backend for entries regex couldn't parse ──
    if LLM_AVAILABLE and polgov_html:
        llm_candidates = collect_llm_candidates(polgov_html, "polgov")
        print(f"\n  [Phase 2] LLM backend — {len(llm_candidates)} ambiguous entries from polgov.html")

        if llm_candidates:
            # Load checkpoint
            existing_persons: list[PEPPerson] = []
            processed_hashes: set[str] = set()
            output_path = "source_b_pep.json"
            if Path(output_path).exists():
                try:
                    with open(output_path, "r", encoding="utf-8") as f:
                        old_data = json.load(f)
                        existing_persons = [PEPPerson(**p) for p in old_data.get("persons", [])]
                        processed_hashes = {p.source_reference for p in existing_persons if p.source_reference}
                    print(f"    [Resume] Loaded {len(existing_persons)} checkpoint records")
                except Exception as e:
                    print(f"    [Resume] Could not load checkpoint: {e}")

            keys = load_api_keys()
            llm_extracted = 0
            llm_skipped = 0

            for i, (entry_text, section) in enumerate(llm_candidates, 1):
                entry_hash = get_entry_hash(entry_text, section)

                # Skip if already processed
                if entry_hash in processed_hashes:
                    matches = [p for p in existing_persons if p.source_reference == entry_hash]
                    if matches:
                        persons.extend(matches)
                        llm_skipped += 1
                        continue

                try:
                    safe_preview = entry_text[:50].encode('ascii', 'replace').decode('ascii')
                    print(f"    [{i}/{len(llm_candidates)}] LLM: {safe_preview}...")
                    person = extract_source_b_entry(entry_text, section, keys)

                    if person:
                        person.source_reference = entry_hash
                        if is_within_last_12_months(person.start_date, person.end_date):
                            persons.append(person)
                            llm_extracted += 1
                            safe_name = person.name.encode('ascii', 'replace').decode('ascii')
                            print(f"      -> {person.role}: {safe_name}")
                except Exception as e:
                    print(f"      [Error] {e}")

            print(f"  [Phase 2] LLM extracted: {llm_extracted}, skipped (cached): {llm_skipped}")
    else:
        print("\n  [Phase 2] LLM not available — skipping ambiguous entries")

    # ── Phase 3: Deduplicate and validate ──
    unique: list[PEPPerson] = []
    seen: set[tuple] = set()
    for p in persons:
        key = (p.name, p.role, p.start_date)
        if key not in seen:
            seen.add(key)
            unique.append(p)

    print(f"\n[Source B] Final: {len(unique)} unique persons (within last 12 months)")
    role_counts: dict[str, int] = {
        "head_of_state": 0, "prime_minister": 0,
        "minister": 0, "governor": 0, "other": 0
    }
    role_key_map = {
        "Head of State": "head_of_state",
        "Prime Minister": "prime_minister",
        "Minister": "minister",
        "Governor": "governor",
        "Senior Official": "other",
        "Other": "other",
    }
    for p in unique:
        k = role_key_map.get(p.role, "other")
        role_counts[k] += 1

    for role_name, count in role_counts.items():
        if count > 0:
            print(f"    {role_name}: {count}")

    return SourceBOutput(
        extraction_date=date.today().isoformat(),
        persons=unique,
        metadata=PEPMetadata(
            total_persons=len(unique),
            by_role=role_counts,
            extraction_notes=(
                f"Declarative extraction from rulers.org local files. "
                f"Dual-backend: regex parser for structured entries, "
                f"LLM (Groq Turbo-V2 with model tiering) for ambiguous entries. "
                f"12-month cutoff: {CUTOFF_DATE.isoformat()}. "
                f"Polish diacritics preserved via UTF-8. "
                f"All records validated through PEPPerson Pydantic schema."
            )
        )
    )


# ─── Networking ───────────────────────────────────────────────────────────────

def fetch_html(url: str) -> str:
    """Fetch page HTML. rulers.org may use legacy encodings — handle gracefully."""
    for attempt in range(3):
        try:
            resp = requests.get(url, headers=HEADERS, timeout=30)
            resp.raise_for_status()
            encoding = resp.encoding or resp.apparent_encoding or "utf-8"
            if encoding.lower() in ("iso-8859-1", "latin_1", "iso-8859-2", "windows-1250"):
                return resp.content.decode("iso-8859-2", errors="replace")
            resp.encoding = encoding
            return resp.text
        except requests.RequestException as e:
            if attempt == 2:
                raise RuntimeError(f"Failed to fetch {url}: {e}")
            print(f"Attempt {attempt + 1} failed, retrying... ({e})")
    return ""


def _load_local_html(filepath: str) -> str:
    """
    Load a local HTML file with correct encoding detection.
    rulers.org pages use ISO-8859-2 (Latin-2) for Polish diacritics,
    but may declare ISO-8859-1 in their meta tags.
    """
    raw = Path(filepath).read_bytes()

    # Detect charset from HTML meta tag
    charset_match = re.search(rb'charset=([A-Za-z0-9_-]+)', raw[:500])
    declared_charset = charset_match.group(1).decode('ascii').lower() if charset_match else None

    # rulers.org declares ISO-8859-1 but actually uses ISO-8859-2 for Polish
    # Try ISO-8859-2 first (it's a superset that handles Polish characters)
    for encoding in ["iso-8859-2", declared_charset or "utf-8", "iso-8859-1", "utf-8"]:
        try:
            return raw.decode(encoding)
        except (UnicodeDecodeError, LookupError):
            continue

    return raw.decode("utf-8", errors="replace")


# ─── Entry point ──────────────────────────────────────────────────────────────

def main():
    print("=" * 60)
    print("Source B - rulers.org Poland PEP Directory (Local Files)")
    print("=" * 60)

    url_to_file = {
        "https://rulers.org/polgov.html": "polgov.html",
        "https://rulers.org/polvoi2.html": "polvoi2.html",
        "https://rulers.org/rulp2.html": "rulp2.html",
    }

    html_pages = []
    for url in SOURCE_URLS:
        local_name = url_to_file.get(url)
        if local_name and Path(local_name).exists():
            print(f"Loading local: {local_name}")
            page_html = _load_local_html(local_name)
            print(f"Loaded {len(page_html):,} bytes")
        else:
            print(f"Fetching: {url}")
            page_html = fetch_html(url)
            print(f"Fetched {len(page_html):,} bytes")

        if page_html:
            html_pages.append((url, page_html))

    result = extract_source_b(html_pages)

    output_path = "source_b_pep.json"
    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(result.model_dump(), f, indent=2, ensure_ascii=False)
    print(f"\n(OK) Output written to {output_path}")
    print(f"  Total persons: {result.metadata.total_persons}")
    for role, count in result.metadata.by_role.items():
        print(f"    {role}: {count}")


if __name__ == "__main__":
    main()
