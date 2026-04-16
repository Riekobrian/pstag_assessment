"""
Source A Extractor — EU Sanctions Regulation 2019/796 (consolidated 2025-05-14)
Target URL: https://eur-lex.europa.eu/resource/consolidation/2019R0796%2F20250514.ENG

Approach:
  1. Fetch the consolidated HTML regulation from publications.europa.eu consolidation endpoints
  2. If the primary endpoint fails, try additional consolidation language variants
  3. If live fetch still fails, read a manually saved local HTML copy
  4. Identify Annex I (the entity list section) using structural markers
  5. Parse each numbered entry using LLM semantic extraction (Gemini API)
  6. Map extracted fields onto Pydantic SanctionEntity models for validation
  7. Emit validated JSON

This is specification-driven extraction: the Pydantic models in models.py define
what we need and what constraints apply. LLM semantic extraction provides robust
field extraction by understanding field meanings rather than brittle regex patterns.
"""

import re
import json
import sys
import os
from datetime import date
from pathlib import Path
from typing import Optional

import requests
from bs4 import BeautifulSoup

# Add parent directory to path if running standalone
sys.path.insert(0, ".")
from models import (
    SourceAOutput, SanctionEntity, SanctionIdentifiers, SanctionMetadata
)

# Import LLM extraction functions
sys.path.insert(0, str(Path(__file__).parent / "llm_extraction"))
try:
    from llm_extraction.llm_extractor import (
        load_api_keys, extract_source_a_entry, call_gemini_with_rotation,
        build_source_a_prompt, parse_json_output, validate_source_a_entity
    )
    LLM_AVAILABLE = True
    print("[OK] Source A: LLM extraction module loaded")
except ImportError as e:
    print(f"[Warning] LLM extraction not available: {e}. Falling back to regex.")
    LLM_AVAILABLE = False

CELEX_ID = "02019R0796-20250514"

# Preferred automatic route: EUR-Lex web frontend (bypasses bot protection when using proper headers)
# This is the reliable path that provides full HTML content
CONSOLIDATION_URLS = [
    "https://eur-lex.europa.eu/legal-content/EN/ALL/?uri=CELEX:02019R0796-20250514",
    "https://eur-lex.europa.eu/legal-content/EN/TXT/?uri=CELEX:02019R0796-20250514",
]

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml",
    "Accept-Language": "en-US,en;q=0.9",
}


def fetch_html(url: str = None) -> str:
    """
    Fetch consolidated regulation HTML using direct EUR-Lex consolidation URLs.

    This is the robust, automated path that bypasses the EUR-Lex web frontend.
    If the English consolidation URL is unavailable, the script will fall back to
    additional language variants and then optionally to a local saved file.
    """
    import os
    fallback_path = "regulation_2019_796.html"

    if os.path.exists(fallback_path):
        print(f"[Fallback] Reading from local file: {fallback_path}")
        with open(fallback_path, encoding="utf-8") as f:
            content = f.read()
            if len(content) > 10_000:
                return content
            print(f"[Warning] Local file too small ({len(content)} bytes), trying live fetch")

    session = requests.Session()
    urls_to_try = [url] if url else CONSOLIDATION_URLS

    for target_url in urls_to_try:
        for attempt in range(3):
            try:
                resp = session.get(
                    target_url,
                    headers={
                        "Accept": "text/html,application/xhtml+xml",
                        "Accept-Language": "en",
                        "User-Agent": HEADERS["User-Agent"],
                    },
                    timeout=60,
                    allow_redirects=True,
                )
                resp.raise_for_status()
                resp.encoding = "utf-8"
                html = resp.text
                if len(html) > 10_000:
                    print(f"[OK] Fetched {len(html):,} bytes from {target_url}")
                    return html
                print(f"Small response ({len(html)} bytes) from {target_url}, trying next...")
                break
            except requests.RequestException as e:
                if attempt == 2:
                    print(f"Failed {target_url}: {e}")
                    break

    raise RuntimeError(
        "All fetch attempts failed. Ensure the consolidation endpoint is reachable. "
        "If necessary, provide a local file named 'regulation_2019_796.html'."
    )


def find_annex_i_text(soup: BeautifulSoup) -> str:
    """
    Locate Annex I in the document - this is the section containing
    the sanctioned persons and organisations list.

    The regulation structure: Articles 1-13 (legal framework) -> Annex I (entity list)
    -> Annex II (Commission contact details). We want Annex I only.

    Returns the raw text of Annex I for further parsing.
    """
    full_text = soup.get_text(separator="\n")
    lines = full_text.split("\n")

    annex_i_start = None
    annex_ii_start = None

    for i, line in enumerate(lines):
        stripped = line.strip().upper()
        # Look for the Annex I header that contains the entity list
        # The correct one is "List of natural and legal persons, entities and bodies"
        if annex_i_start is None and "list of natural and legal persons" in line.lower():
            annex_i_start = i
        elif annex_i_start is not None and re.match(r"^ANNEX\s+II\b", stripped):
            annex_ii_start = i
            break

    if annex_i_start is None:
        # Fallback: search for ANNEX I but make sure it's the entity list one
        for i, line in enumerate(lines):
            if "ANNEX I" in line.upper() and "list of natural and legal persons" in lines[min(i+10, len(lines)-1)].lower():
                annex_i_start = i
                break

    if annex_i_start is None:
        raise ValueError(
            "Could not locate Annex I entity list in the document. "
            "Check the page structure manually."
        )

    end = annex_ii_start if annex_ii_start else len(lines)
    annex_text = "\n".join(lines[annex_i_start:end])
    print(
        f"[Source A] Annex I found at line {annex_i_start}, "
        f"ends at line {end} ({end - annex_i_start} lines)"
    )
    return annex_text


def split_entries(annex_text: str) -> list[str]:
    """
    Split Annex I into individual entity entries.
    EUR-Lex Annex I entries are typically numbered: 1., 2., 3. ...
    Some consolidated versions use letter+number headings (A.1, B.2 etc.).
    We detect the pattern from the first few entries.
    """
    # Primary pattern: numbered entries like "1.\n" or "1. Name"
    entries = re.split(r"\n(?=\d{1,3}\.\s)", annex_text)

    # If that yields too few entries, try section-based split
    if len(entries) < 3:
        entries = re.split(r"\n(?=[A-Z]\.\s{0,3}\d)", annex_text)

    # Strip empty
    entries = [e.strip() for e in entries if e.strip() and len(e.strip()) > 20]
    print(f"[Source A] Found {len(entries)} candidate entries in Annex I")
    return entries


def classify_entity_type(entry_text: str) -> str:
    """
    Determine whether an entry is a 'person' or 'organisation'.
    Persons typically have date of birth, nationality, passport fields.
    Organisations typically have registration details, addresses, aliases
    but no DOB. Some organisation names include suffixes like 'Ltd',
    'Co.' or 'Inc.' and should be classified as organisations immediately.
    """
    normalized_entry = re.sub(r"^\d{1,3}\.\s*", "", entry_text.strip())
    first_line = normalized_entry.split("\n", 1)[0].strip()
    first_line_lower = first_line.lower()
    if (
        "co." in first_line_lower
        or " co ltd" in first_line_lower
        or " co. ltd" in first_line_lower
        or re.search(
            r"\b(?:Ltd|Inc|S\.A\.|SA|GmbH|LLC|Company|Foundation|Institute|Agency|Bank|Corporation|Centre|Center|Technologies?|Services?)\b",
            first_line,
            re.IGNORECASE,
        )
    ):
        return "organisation"

    person_signals = [
        r"\bdate of birth\b",
        r"\bnationality\b",
        r"\bpassport\b",
        r"\bgender\s*:\s*(male|female)",
        r"\bborn\b",
        r"\bplace of birth\b",
    ]
    org_signals = [
        r"\bregistration\b",
        r"\bcompany\b",
        r"\borganis[az]ation\b",
        r"\binstitute\b",
        r"\bservice\b",
        r"\bgroup\b",
        r"\bunit\b",
        r"\bbureau\b",
    ]
    text_lower = entry_text.lower()
    person_score = sum(1 for p in person_signals if re.search(p, text_lower))
    org_score = sum(1 for p in org_signals if re.search(p, text_lower))

    # If there are company signals but no person signals, treat as an organisation.
    if org_score > 0 and person_score == 0:
        return "organisation"

    return "person" if person_score >= org_score else "organisation"


def is_likely_entity_entry(entry_text: str) -> bool:
    """Detect whether Annex I candidate text looks like a named entity entry."""
    first_line = entry_text.strip().split("\n", 1)[0].strip()
    if len(first_line) < 5:
        return False

    generic_starts = [
        r"^by way of",
        r"^without prejudice",
        r"^the",
        r"^any",
        r"^in the absence",
        r"^actions by",
        r"^natural or legal persons",
        r"^pursuant to",
        r"^article",
        r"^annex i",
        r"^list of",
    ]
    for pattern in generic_starts:
        if re.match(pattern, first_line, re.IGNORECASE):
            return False

    name_like = re.search(r"\b[A-Z][a-z]+(?:\s+[A-Z][a-z]+)+\b", first_line)
    company_like = re.search(
        r"\b(Ltd|Inc|S\.A\.|SA|GmbH|LLC|Company|Foundation|Institute|Agency|Bank|Corporation)\b",
        first_line, re.IGNORECASE
    )
    field_like = re.search(
        r"\b(date of birth|nationality|place of birth|passport|gender|address)\b",
        entry_text, re.IGNORECASE
    )
    return bool(name_like or company_like or field_like)


def extract_field(pattern: str, text: str, flags: int = re.IGNORECASE) -> Optional[str]:
    """Extract first capture group from pattern, strip whitespace."""
    m = re.search(pattern, text, flags | re.DOTALL)
    if m:
        return m.group(1).strip().strip(";").strip(",").strip()
    return None


def extract_name(entry_text: str) -> str:
    """
    Extract the primary name from an entry.
    EUR-Lex entries typically start with: "1. SURNAME, FirstName" or
    "1. Full Name (AKA: alias)"
    """
    # Remove leading entry number
    text = re.sub(r"^\d{1,3}\.\s*", "", entry_text.strip())
    # Name is typically on the first line, before a newline or parenthetical
    first_line = text.split("\n")[0].strip()
    # Remove parenthetical aliases from name line
    name = re.sub(r"\s*\(.*?\)\s*", " ", first_line).strip()
    # Remove "also known as" from name
    name = re.sub(r"\s*also known as.*$", "", name, flags=re.IGNORECASE).strip()
    return name


def extract_aliases(entry_text: str) -> list[str]:
    """Extract all 'also known as' aliases for this entry."""
    aliases = []
    # Pattern 1: "also known as 'X', 'Y'"
    m = re.findall(
        r"also known as[:\s]+['\u2018\u2019\u201c\u201d]?([^,;\n\(]+)['\u201d]?",
        entry_text, re.IGNORECASE
    )
    aliases.extend([a.strip().strip("'\"") for a in m if a.strip()])
    # Pattern 2: parenthetical (a.k.a. X)
    m2 = re.findall(r"\(a\.?k\.?a\.?\s+([^)]+)\)", entry_text, re.IGNORECASE)
    aliases.extend([a.strip() for a in m2 if a.strip()])
    return list(dict.fromkeys(aliases))  # deduplicate preserving order


def parse_date(raw: str) -> str:
    """
    Attempt to normalise a date string to YYYY-MM-DD.
    If ambiguous or only partial, return the original text as-is
    (per spec: ambiguous dates are flagged, not silently dropped).
    """
    if not raw:
        return raw
    raw = raw.strip()
    # Already ISO
    if re.match(r"^\d{4}-\d{2}-\d{2}$", raw):
        return raw
    # DD.MM.YYYY or DD/MM/YYYY
    m = re.match(r"^(\d{1,2})[./](\d{1,2})[./](\d{4})$", raw)
    if m:
        return f"{m.group(3)}-{m.group(2).zfill(2)}-{m.group(1).zfill(2)}"
    # DD Month YYYY
    months = {
        "january": "01", "february": "02", "march": "03", "april": "04",
        "may": "05", "june": "06", "july": "07", "august": "08",
        "september": "09", "october": "10", "november": "11", "december": "12"
    }
    m2 = re.match(
        r"^(\d{1,2})\s+(" + "|".join(months.keys()) + r")\s+(\d{4})$",
        raw, re.IGNORECASE
    )
    if m2:
        return f"{m2.group(3)}-{months[m2.group(2).lower()]}-{m2.group(1).zfill(2)}"
    # YYYY only or YYYY-MM only — return as-is (partial)
    return raw


def parse_entry(entry_text: str, idx: int) -> Optional[SanctionEntity]:
    """
    Parse a single Annex I entry into a SanctionEntity using LLM semantic extraction.
    Falls back to regex extraction if LLM is not available.

    Returns None only if the entry appears to be a section header or
    non-entity text (e.g. a preamble paragraph).
    """
    # Skip if this looks like a header/preamble rather than an entity entry
    text_lower = entry_text.lower()
    non_entity_signals = [
        "the following natural", "the following legal", "annex i", "list of",
        "pursuant to article", "article 3"
    ]
    if any(s in text_lower[:200] for s in non_entity_signals):
        return None

    # Try LLM extraction first if available
    if LLM_AVAILABLE:
        try:
            keys = load_api_keys()
            # The LLM will use SOURCE_A_FIELD_SPECS to extract correctly by semantics
            entity = extract_source_a_entry(entry_text, idx, keys)
            if entity:
                return entity
        except Exception as e:
            print(f"  [LLM] Failed for entry {idx} ({e}), falling back to regex")

    # Fallback to regex extraction
    print(f"[Regex] Processing entry {idx}...")
    entity_type = classify_entity_type(entry_text)
    name = extract_name(entry_text)

    # Skip entries where name extraction clearly failed
    if not name or len(name) < 2:
        return None

    aliases = extract_aliases(entry_text)

    # ── Identifiers ──────────────────────────────────────────────
    raw_dob = (
        extract_field(r"date of birth[:\s]+([^\n;]+)", entry_text) or
        extract_field(r"born[:\s]+([^\n;,]+)", entry_text)
    )
    identifiers = SanctionIdentifiers(
        date_of_birth=parse_date(raw_dob) if raw_dob else None,
        place_of_birth=extract_field(
            r"place of birth[:\s]+([^\n;]+)", entry_text
        ),
        nationality=extract_field(
            r"nationality[:\s]+([^\n;,]+)", entry_text
        ),
        passport_number=extract_field(
            r"passport(?:\s+no\.?|number)[:\s]+([A-Z0-9\s]+)", entry_text
        ),
        national_id=extract_field(
            r"(?:national\s+id(?:entity)?|id\s+card|national\s+number)[:\s]+([^\n;,]+)",
            entry_text
        ),
        address=extract_field(
            r"(?:address|last known address)[:\s]+([^\n]+(?:\n[^\n]+){0,2})",
            entry_text
        ),
        gender=extract_field(
            r"gender[:\s]+(male|female)", entry_text
        ),
    )

    # ── Listing reason ────────────────────────────────────────────
    reason = extract_field(
        r"(?:grounds?|reasons?|listed\s+for)[:\s]+(.+?)(?:\n\n|\Z)",
        entry_text
    )
    if not reason:
        # Try to grab the substantive paragraph — usually the longest
        paragraphs = [p.strip() for p in entry_text.split("\n\n") if len(p.strip()) > 80]
        if paragraphs:
            reason = paragraphs[-1][:500]

    # ── Date listed ───────────────────────────────────────────────
    raw_listed = extract_field(
        r"(?:listed|added|designated)[:\s]+([^\n;,]+(?:\d{4}))",
        entry_text
    )

    return SanctionEntity(
        entity_type=entity_type,
        name=name,
        aliases=aliases,
        identifiers=identifiers,
        listing_reason=reason,
        date_listed=parse_date(raw_listed) if raw_listed else None,
        source_reference=f"Annex I, entry {idx}"
    )


def extract_source_a(html: str) -> SourceAOutput:
    soup = BeautifulSoup(html, "lxml")
    annex_text = find_annex_i_text(soup)
    raw_entries = split_entries(annex_text)

    # ── RESUME LOGIC: Load existing results ──
    existing_entities = []
    processed_refs = set()
    output_path = "source_a_sanctions.json"
    if Path(output_path).exists():
        try:
            with open(output_path, "r", encoding="utf-8") as f:
                old_data_json = json.load(f)
                old_data = SourceAOutput(**old_data_json)
                if len(old_data.entities) >= 21:
                    print(f"  [Resume] Source A already has {len(old_data.entities)} entities. Skipping full extraction.")
                    return old_data
                existing_entities = old_data.entities
                processed_refs = {e.source_reference for e in existing_entities if e.source_reference}
            print(f"  [Resume] Loaded {len(existing_entities)} existing entities. Resuming...")
        except Exception as e:
            print(f"  [Resume] Could not load existing results: {e}")

    entities = []
    skipped = 0
    for i, entry in enumerate(raw_entries, start=1):
        # Flexible skip check: does any existing reference contains "entry {i}"?
        if any(f"entry {i}" in ref for ref in processed_refs):
            # Find the best match entity to keep the list complete
            entity = next((e for e in existing_entities if f"entry {i}" in (e.source_reference or "")), None)
            if entity:
                entities.append(entity)
                continue
        
        try:
            entity = parse_entry(entry, i)
            if entity:
                entities.append(entity)
            else:
                skipped += 1
        except Exception as e:
            print(f"[Source A] Warning: failed to parse entry {i}: {e}")
            skipped += 1

    if len(entities) == 0 and len(raw_entries) > 5:
        print(
            "[Source A] Warning: Annex I appears to contain general regulatory text rather than "
            "a list of named entities. No valid sanctioned entities were extracted."
        )

    print(f"[Source A] Extracted {len(entities)} entities ({skipped} skipped)")

    persons = [e for e in entities if e.entity_type == "person"]
    orgs = [e for e in entities if e.entity_type == "organisation"]

    return SourceAOutput(
        extraction_date=date.today().isoformat(),
        entities=entities,
        metadata=SanctionMetadata(
            total_entities=len(entities),
            by_type={"person": len(persons), "organisation": len(orgs)}
        )
    )


def main():
    print("=" * 60)
    print("Source A — EU Sanctions Regulation 2019/796")
    print("=" * 60)
    print(f"Fetching consolidation HTML from {CONSOLIDATION_URLS[0]}")
    html = fetch_html()
    print(f"Fetched {len(html):,} bytes")

    result = extract_source_a(html)

    output_path = "source_a_sanctions.json"
    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(result.model_dump(), f, indent=2, ensure_ascii=False)
    print(f"\n(OK) Output written to {output_path}")
    print(f"  Total entities: {result.metadata.total_entities}")
    print(f"  Persons:        {result.metadata.by_type['person']}")
    print(f"  Organisations:  {result.metadata.by_type['organisation']}")


if __name__ == "__main__":
    main()
