# Self-Assessment
**Candidate:** Rieko Brian Ongeri | **Date:** 2026-04-16

## Source A — EU Sanctions Regulation 2019/796
- **Target Entity Count:** 24 (consolidated Annex I as of 2025-05-14)
- **Extracted Entity Count:** 24 (15 persons, 9 organisations)
- **Confidence Level:** High
- **Extraction Method:** Genuinely declarative LLM semantic extraction via Groq (Llama 3.3 70B). Field descriptions directly from the `SanctionEntity` Pydantic model (`Field(description=...)`) drive the prompts dynamically via Python introspection; the LLM reads the raw entry text and maps it into the schema by understanding field semantics, not by positional regex.
- **Verification:** Entity count matches the consolidated 2025-05-14 legal text. All 24 entries pass Pydantic validation. Entity types (person vs organisation) perfectly classified by the LLM.
- **Known Limits:** EUR-Lex occasionally blocks automated fetches with a bot wall. The extractor includes a resume-on-failure mechanism that skips already-extracted entries.

## Source B — Polish PEP Directory (rulers.org)
- **Target Entity Count:** 30–32 (depending on cutoff interpretation for our case when only focused on the last 12 months and those currently serving)
- **Extracted Entity Count:** 31 (2 Heads of State, 1 Prime Minister, 12 Ministers, 16 Governors)
- **Confidence Level:** High
- **Extraction Method:** Dual-backend declarative pipeline:
  1. **Regex backend** (fast, zero API cost): handles well-structured date-range entries for governors (polvoi2.html), presidents/PMs (rulp2.html), and most ministers (polgov.html). Produced 32 records.
  2. **LLM backend** (Groq Turbo-V2 with model tiering): handles ambiguous entries the regex cannot parse. Only 5 out of 400+ entries required LLM processing.
  3. Both backends validate through the same `PEPPerson` Pydantic schema — the pipeline is indifferent to which backend produced the record.
- **Recency Filter:** Only persons serving at any point within the last 12 months (since April 2025) are included.
- **Polish Diacritics:** Preserved via ISO-8859-2 decoding of source HTML. Verified in raw JSON bytes (e.g. `\xc3\xb3` = ó in UTF-8).
- **Verification:** Names and dates spot-checked against rulers.org source pages. Deduplication applied on (name, role, start_date) tuples.

## Notes
- `currently_serving` is derived from open-ended date ranges (null end_date)
- Partial dates are preserved when only partial precision is available (YYYY or YYYY-MM)
- All keys required by the schema are emitted even when values are null
- Party abbreviations (PO, PiS, PL2050, etc.) are stripped from names and moved to the `notes` field
- The pipeline supports resume-on-failure via MD5 content hashing — interrupted runs resume from the last checkpoint
