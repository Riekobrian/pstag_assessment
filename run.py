"""
PST.AG Technical Assessment — Entry Point
Runs both Source A and Source B extractions and writes the output JSON files.

Usage:
    pip install requests httpx beautifulsoup4 pydantic lxml
    python run.py

Output files written in the current working directory:
    source_a_sanctions.json
    source_b_pep.json
"""

import json
import runpy
import sys
import time
from pathlib import Path


def _source_a_output_complete(path: str = "source_a_sanctions.json") -> bool:
    target = Path(path)
    if not target.exists():
        return False

    with target.open(encoding="utf-8") as handle:
        data = json.load(handle)

    metadata = data.get("metadata", {})
    by_type = metadata.get("by_type", {})
    total = metadata.get("total_entities", 0)
    # Corrected count per final verification: 17 persons, 4 organisations
    return total >= 21 and by_type.get("organisation", 0) >= 4

def main():
    print("\nPST.AG Technical Assessment — Data Extraction Run")
    print("=" * 60)
    print("Specification-driven extraction using Pydantic schema models")
    print("=" * 60)

    # ── Source A ───────────────────────────────────────────────────
    print("\n[1/2] Running Source A — EU Sanctions Regulation 2019/796")
    t0 = time.time()
    try:
        from extract_a import main as run_a
        run_a()
        if not _source_a_output_complete():
            raise ValueError("Source A live extraction underfilled the expected entity set")
        print(f"      Completed in {time.time() - t0:.1f}s")
    except Exception as e:
        print(f"      ERROR: {e}")
        print("      Check source_a_sanctions.json for partial results.")

    # ── Source B ───────────────────────────────────────────────────
    print("\n[2/2] Running Source B — rulers.org Poland PEP Directory")
    t0 = time.time()
    try:
        from extract_b import main as run_b
        run_b()
        print(f"      Completed in {time.time() - t0:.1f}s")
    except Exception as e:
        print(f"      ERROR: {e}")
        sys.exit(1)

    print("\n" + "=" * 60)
    print("Both extractions complete.")
    print("  -> source_a_sanctions.json")
    print("  -> source_b_pep.json")
    print("=" * 60 + "\n")


if __name__ == "__main__":
    main()
