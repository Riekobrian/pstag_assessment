# Pre-Extraction Planning Checklist — PST.AG Technical Assessment
**Candidate:** Rieko Brian Ongeri | **Date:** 2026-04-15

## 1. Schema Contract Analysis
### Source A — EU Sanctions Regulation
- **Required fields:** entity_type, name, identifiers (optional subfields), listing_reason, source_reference
- **Optional fields:** aliases, date_listed
- **Data types:** All strings, null for missing values
- **Validation rules:** entity_type must be "person" or "organisation"
- **Output format:** JSON with metadata counts

### Source B — Polish PEP Directory
- **Required fields:** name, role, start_date, end_date, currently_serving
- **Optional fields:** role_detail, birth_year, notes
- **Data types:** Strings for dates (partial allowed), boolean for currently_serving
- **Validation rules:** role must be from predefined categories
- **Output format:** JSON with metadata counts and extraction notes

## 2. Source Structure Assessment
### Source A — Structured Legal Document
- **Page type:** Official EU regulation HTML
- **Data location:** Annex I section only (entity list)
- **Entry boundaries:** Numbered paragraphs (1., 2., 3.)
- **Field markers:** Semantic labels identified by LLM
- **Risks:** Bot blocking, consolidation version changes
- **Approach:** Declarative LLM semantic extraction (primary), regex fallback

### Source B — Semi-Structured Directory
- **Page type:** Plain-text HTML directory
- **Data location:** Multiple sections (ministers, governors, presidents)
- **Entry boundaries:** Structural section headers + line breaks
- **Field markers:** Informal dates and roles identified by LLM semantic specs
- **Risks:** Page layout changes, encoding issues, partial dates, API quota exhaustion on 400+ entries
- **Approach:** Hybrid Declarative pipeline — regex backend for structured entries (zero cost), LLM semantic extraction for ambiguous entries only

## 3. Technical Requirements
### Data Handling
- **Nulls:** Preserve as `null`, never drop optional fields
- **Ambiguous dates:** Keep partial dates (YYYY, YYYY-MM) rather than guess
- **Encoding:** UTF-8 with Polish diacritics preserved
- **Filtering:** Source B only — last 12 months recency filter

### Error Handling
- **Network failures:** Retry with fallbacks, clear error messages
- **Parsing failures:** Log issues, continue with partial results
- **Schema violations:** Validate with Pydantic, report validation errors

## 4. Risk Assessment
### Page Structure Changes
- **Source A:** Annex I boundaries may shift → fallback to content-based detection
- **Source B:** Section headers may change → role inference from context
- **Mitigation:** Multiple detection patterns, section-aware parsing

### Data Quality Issues
- **Missing fields:** Emit as `null` with schema preservation
- **Ambiguous values:** Preserve raw text rather than coerce
- **Encoding problems:** Multiple encoding fallbacks

## 5. Extraction Strategy
### Source A Strategy
1. Fetch consolidated regulation HTML
2. Locate Annex I by heading + content markers
3. Split into numbered entries
4. Extract fields by label patterns
5. Classify person vs organisation
6. Validate and emit JSON

### Source B Strategy
1. Fetch multiple page URLs (polgov.html, polvoi2.html, rulp2.html)
2. Parse each page with page-specific logic
3. Extract entries with date/name/role patterns
4. Apply 12-month recency filter
5. Clean names and preserve diacritics
6. Validate and emit JSON

## 6. Success Criteria
### Completeness
- Source A: All Annex I entities extracted
- Source B: All current/recent office holders within 12 months

### Accuracy
- Field extraction matches source content
- Dates preserved in correct format
- Names and diacritics intact

### Reliability
- Extraction runs without manual intervention
- Clear error messages for failures
- Schema validation passes

## 7. AI-Assisted Workflow Plan
### When to Use AI
- Schema design and validation logic
- Regex pattern suggestions
- Debugging parsing failures
- Encoding and diacritics handling

### How to Use AI
- Provide HTML snippets and failure symptoms
- Test suggestions against real source data
- Validate outputs manually
- Document iterations in conversation log

## 8. Testing Plan
### Unit Tests
- Schema validation with sample data
- Date parsing edge cases
- Encoding preservation

### Integration Tests
- Full extraction pipeline
- Output count verification
- Manual spot-checks against source

### Regression Tests
- Save sample HTML for future comparison
- Monitor for count changes
- Document known edge cases

## 9. Timeline Estimate
- Schema design: 15 minutes
- Source A extraction: 25 minutes
- Source B extraction: 25 minutes
- Testing and documentation: 15 minutes
- **Total:** ~80 minutes

## 10. Go/No-Go Decision
### Ready to proceed if:
- Schema contracts are clear
- Source URLs are accessible
- Basic HTML structure is understood
- AI tools are available for assistance

### Stop and reassess if:
- Source pages are completely inaccessible
- Schema requirements are ambiguous
- Page structure is radically different from expected