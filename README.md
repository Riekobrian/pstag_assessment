# PST.AG Technical Assessment
**Candidate:** Rieko Brian Ongeri

## Setup & Run
```bash
# Install dependencies
pip install requests pydantic bs4 lxml

# The extractors depend on Groq API for declarative semantic extraction.
# Ensure your GROQ_API_KEY is set in llm_extraction/.env
python run.py
```

`run.py` orchestrates the extraction for both Source A and Source B, writing `source_a_sanctions.json` and `source_b_pep.json`.

This repo implements a **declarative, specification-driven extraction pipeline**. The Pydantic models in `models.py` serve as the source of truth; at runtime, the system uses LLM-based semantic extraction (Groq Llama 3.3 70B) to map source content into these models by understanding field meanings rather than relying on brittle regex patterns.

## 1. What AI tools did you use and for what?
I adopted a two-pronged AI strategy:
- **Development & Refactoring (Claude/Deepmind Assistant):** Used as a pair-programmer to design the data contracts (`models.py`), build the resilience layer, and refactor imperative regex scripts strictly into a single-source-of-truth declarative pipeline.
- **Runtime Semantic Extraction (Groq API - Llama 3.3 70B):** Used dynamically within the execution pipeline to extract complex unstructured data where regex fails (e.g., classifying unlabelled organisations, extracting fields from prose reasons). Groq was chosen for its unparalleled inference speed.

## 2. What did AI handle well, and where did it struggle?
**Handled Well:** The runtime LLM excelled at *semantic classification* and *noise filtering*. For example, when Source A listed a company without an "organisation" label, the LLM understood context strings like "Co. Ltd" and classified it perfectly, whereas rigid regex algorithms failed. AI also correctly ignored parenthetical annotations injected into names in Source B.

**Struggled:** The LLM initially struggled with rate limits (429 errors from free-tier APIs) and hallucinating keys that weren't in the schema.
**Solution:** I built a `Turbo-V2` Resilience Engine that rotates API keys and uses header-aware token-bucket throttling. I fixed the hallucinations by making the architecture fully declarative: the prompt dynamically reads the `Pydantic` model via introspection, forcing the LLM to output only the keys and data types explicitly requested.

## 3. What did you do with traditional code vs. what did you delegate to AI?
- **Traditional Code (The Rigging):** Fetching HTML, encoding detection (e.g., ISO-8859-2 for Polish diacritics), chunking text into separate entries, and a zero-cost regex fallback layer for perfectly tabular rows.
- **Delegated to AI (The Engine):** Extracting ambiguous semantic variables. Instead of writing brittle XPaths or 50+ conditional regex heuristics for poorly formatted entries, I passed the raw chunk to the LLM with the Pydantic spec. The LLM reads it like a human and maps it into the JSON schema.

## 4. How would you adapt this for a third unknown source?
Because the architecture is genuinely **declarative** (Schema-as-Spec), adding "Source C" requires exactly zero new extraction scripts:
1. Define the data contract in `models.py` (e.g., `class SourceCEntity(BaseModel):`).
2. Add explicit data instructions into Pydantic using `Field(description="...")`.
3. The prompt builder uses Python introspection (`model.model_fields.items()`) to automatically generate the LLM prompt.
4. Pass the raw text loop to the builder. The system handles the mapping automatically.

## 5. How did you allocate your time?
- **Analysis & Schema Definition (models.py):** ~30 mins (Setting the core Pydantic contracts and understanding the HTML source variations).
- **Core Scraping & Rigging (extract_a.py / extract_b.py):** ~30 mins (Handling encodings, diacritics, chunking text).
- **Declarative AI Orchestration & Refactoring:** ~45 mins (Building the prompt introspection logic, the rate-limiting resilience engine, and replacing manual dicts with a strict model-driven pipeline).
- **Testing & Documentation:** ~15 mins.
**Total:** ~2 Hours.
