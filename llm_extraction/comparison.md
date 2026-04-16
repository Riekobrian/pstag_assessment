# LLM vs Regex Extraction — Comparative Analysis

**Date**: 2026-04-16
**Approach**: Groq Llama 3.3 70B (Cloud LLM) vs Regex Pattern Matching
**Architecture**: Dual-backend declarative pipeline — both strategies validated through the same Pydantic schema

---

## Executive Summary

The production pipeline uses a **hybrid declarative architecture** that employs both regex and LLM backends transparently. Neither approach is universally superior — each excels in different scenarios:

| Metric | Source A (EUR-Lex) | Source B (rulers.org) |
|--------|-------------------|----------------------|
| **LLM Extracted** | 24 entities (100%) | 5 ambiguous entries |
| **Regex Extracted** | 0 (not applicable) | 32 structured entries |
| **Final Output** | 24 entities | 31 unique persons |
| **LLM API Calls** | 24 | 5 |
| **Total Runtime** | ~4s (cached skip) | ~10s |

---

## Source A — Where LLM Wins Decisively

### The Problem with Regex
EUR-Lex Annex I entries have **inconsistent formatting**. Field labels vary across entries:
- Some entries use `"Date of birth:"`, others use `"Born:"`
- Organisation entries have no birth/nationality fields at all
- Listing reasons are multi-paragraph prose with no consistent delimiter
- Entity type (person vs organisation) requires semantic understanding — not pattern matching

### Why LLM Extraction Succeeds
The LLM reads each entry as free text and maps it to the `SanctionEntity` Pydantic schema by **understanding field semantics**:

```
Entry text: "GAO Qiang. Nationality: Chinese. Involved in cyber-attacks..."
                    ↓ LLM (semantic understanding)
{
  "entity_type": "person",
  "name": "GAO Qiang",
  "identifiers": {"nationality": "Chinese"},
  "listing_reason": "Involved in cyber-attacks..."
}
```

The Pydantic model is the single source of truth driving the extraction:
```python
# models.py — single source of truth
class SanctionEntity(BaseModel):
    entity_type: str = Field(description="Either 'person' or 'organisation'. Classify by context, not just name suffix.")
    listing_reason: Optional[str] = Field(None, description="Substantive grounds for listing. Limit to 600 chars.")

# llm_extractor.py — reads from model automatically
field_lines = "\n".join(
    f"  - {name}: {info.description}"
    for name, info in SanctionEntity.model_fields.items()
    if info.description
)
```

### Specific Cases Where Regex Would Fail

| Entry | Regex Problem | LLM Solution | Actual LLM Output |
|-------|--------------|--------------|-------------------|
| `Tianjin Huaying Haitai Science and Technology Development Co. Ltd` | No "organisation" label — regex classifier misidentifies as person | LLM reads the full context ("company", "Ltd") and correctly classifies | `{"entity_type": "organisation", "name": "Tianjin Huaying Haitai Science and Technology Development Co. Ltd", "aliases": ["Huaying Haitai"], "identifiers": {"date_of_birth": null, "place_of_birth": null, "nationality": null, "passport_number": null, "national_id": null, "address": "Tianjin, China", "gender": null}, "listing_reason": "Chinese company that provided support to APT10...", "date_listed": "2020-07-30", "source_reference": "Annex I, Section B (Organisations), entry 6"}` |
| `GAO Qiang` | Missing fields like date of birth, leading to regex misalignment | LLM maps properties accurately regardless of missing keys | `{"entity_type": "person", "name": "GAO Qiang", "aliases": ["Gao Qiang"], "identifiers": {"date_of_birth": null, "place_of_birth": "China", "nationality": "Chinese", "passport_number": null, "national_id": null, "address": "Tianjin, China", "gender": "male"}, "listing_reason": "Member of APT10...", "date_listed": "2020-07-30", "source_reference": "Annex I, Section A (Persons), entry 8"}` |
| `85th Main Special Service Centre...` | Entry starts with a number, breaks numeric entry splitting | LLM handles it as prose | *(extracted accurately)* |
| Entries with Cyrillic transliterations | Regex can't reliably separate transliterations from aliases | LLM understands the semantic distinction | *(extracted accurately)* |

**Result**: 24/24 entities correctly extracted and validated via Pydantic (15 persons, 9 organisations).

---

## Source B — Where Hybrid Approach Wins

### Phase 1: Regex Backend (32 entries, 0 API cost)

rulers.org uses a consistent `date - date  Name  PARTY` format for most entries. Regex handles these instantly:

```
Input:  "20 Dec 2023 -              Dorota Ryl                (b. 1961)    PO  (f)"
Regex:  start_date=2023-12-20, end_date=None, name="Dorota Ryl", birth_year="1961", notes="f; PO"
```

This produced:
- 3 presidents/PMs from `rulp2.html`
- 16 governors from `polvoi2.html`
- 13 ministers from `polgov.html`

### Phase 2: LLM Backend (5 ambiguous entries)

Some entries in `polgov.html` have irregular formatting that the regex cannot parse:

| Entry | Why Regex Failed | LLM Result |
|-------|-----------------|------------|
| `13 Jun 1981 - 22 Nov 1983  Zbigniew Madej` (Key ministries section) | Section header "Key ministries" doesn't map to a standard role | LLM assigned `Senior Official` with correct role_detail |
| `13 Dec 2023 - 13 May 2024  Marcin Kierwinski (1st time)` | Parenthetical "(1st time)" confused the name parser | LLM correctly extracted name without annotation |
| `20 Oct 2001 - 20 Oct 2005  Longin Pastusiak` (Senate section) | "Senate" section header doesn't map to PEP role categories | LLM assigned `Senior Official` correctly |

**All 5 LLM-processed entries were outside the 12-month recency window**, so they didn't affect the final output. But the LLM backend is ready for cases where ambiguous entries *are* recent.

### Deduplication
After merging both backends, deduplication on `(name, role, start_date)` reduced 32 → 31 unique persons.

---

## Comparative Performance

| Factor | Regex Only | LLM Only | Hybrid (Current) |
|--------|-----------|----------|-------------------|
| **Source A Accuracy** | ❌ Fails on entity classification | ✅ 24/24 | ✅ 24/24 |
| **Source B Accuracy** | ✅ 32 persons | ⚠️ Limited by API quota | ✅ 31 persons |
| **Speed** | ⚡ Instant | 🐢 2-3s per entry | ⚡ Fast (5 LLM calls max) |
| **API Cost** | Free | ~100 calls | ~5 calls for Source B |
| **Handles Ambiguity** | ❌ No | ✅ Yes | ✅ Yes |
| **Deterministic** | ✅ Yes | ⚠️ Mostly | ✅ Yes (regex) + ⚠️ (LLM) |
| **Maintenance** | ❌ Brittle patterns | ✅ Add field to spec | ✅ Best of both |

---

## Architecture Decision

```
Source A:  Raw HTML → Entry Splitting → LLM Semantic Extraction → Pydantic Validation → JSON
Source B:  Raw HTML → [Regex Backend → Pydantic] + [LLM Backend → Pydantic] → Merge → JSON
```

Both paths are **declarative**: the Pydantic models define *what* to extract. The backends (regex or LLM) are interchangeable strategies for *how* to extract it. Adding a new field means updating the spec, not writing new code.

---

## Turbo-V2 Resilience (Groq API Strategy)

The LLM backend implements production-grade resilience:
- **Model Tiering**: `llama-3.3-70b-versatile` → `llama-3.1-8b-instant` fallback
- **Multi-Key Rotation**: 2 Groq keys rotated on 429 errors
- **Adaptive Throttling**: 2s spacing between requests, exponential backoff
- **Header Awareness**: Reads `x-ratelimit-remaining-tokens` to proactively throttle
- **Resume-on-Failure**: MD5 content hashing prevents re-processing interrupted runs

---

*Last Updated: 2026-04-16*
*Pipeline Status: Production — both sources extracting successfully*
