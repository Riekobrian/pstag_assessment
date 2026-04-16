"""
Extraction specification models for PST.AG Technical Assessment.

These Pydantic models are the single source of truth for what data we extract
and what constraints it must satisfy. The extraction code is a downstream
executor of these specifications — not the other way around.
"""

from __future__ import annotations
from datetime import date
from typing import Optional
from pydantic import BaseModel, Field, field_validator


# ─────────────────────────────────────────────
# SOURCE A  —  EU Sanctions (Regulation 2019/796)
# ─────────────────────────────────────────────

class SanctionIdentifiers(BaseModel):
    """
    Structured identifying information for a sanctioned entity.
    All fields are optional — absence is recorded as null, never silently dropped.
    Ambiguous values are preserved as the original text rather than coerced.
    """
    date_of_birth: Optional[str] = Field(
        default=None,
        description="Date of birth in YYYY-MM-DD format if parseable; preserve original text if ambiguous (e.g. '27.5.1972')."
    )
    place_of_birth: Optional[str] = Field(
        default=None,
        description="City and/or country as stated in the source."
    )
    nationality: Optional[str] = Field(
        default=None,
        description="Nationality as stated (e.g. 'Russian', 'Chinese')."
    )
    passport_number: Optional[str] = Field(
        default=None,
        description="Passport number if listed (alphanumeric)."
    )
    national_id: Optional[str] = Field(
        default=None,
        description="National identity card number if listed."
    )
    address: Optional[str] = Field(
        default=None,
        description="Last known address if listed."
    )
    gender: Optional[str] = Field(
        default=None,
        description="Gender as stated: 'male' or 'female'. Null if not stated."
    )


class SanctionEntity(BaseModel):
    """
    A single sanctioned person or organisation listed in Annex I of EU 2019/796.
    """
    entity_type: str = Field(
        description="Either 'person' (natural person with DOB/nationality) or 'organisation' (company, agency, unit, group). Classify by context, not just name suffix."
    )
    name: str = Field(
        description="The official name exactly as written in the source, including transliterations (e.g. Cyrillic transliterations in brackets)."
    )
    aliases: list[str] = Field(
        default_factory=list,
        description="All alternate names, 'also known as' names, online monikers, or transliterations listed. Return as a JSON array of strings."
    )
    identifiers: SanctionIdentifiers = Field(
        default_factory=SanctionIdentifiers,
        description="All structured identifying fields for this entity."
    )
    listing_reason: Optional[str] = Field(
        default=None,
        description="The substantive grounds for listing - what the person/organisation did to be sanctioned. Preserve key facts. Limit to 600 chars."
    )
    date_listed: Optional[str] = Field(
        default=None,
        description="Original listing date if discernible (e.g. '30.7.2020' or '24.6.2024')."
    )
    source_reference: Optional[str] = Field(
        default=None,
        description="The Annex I entry number as 'Annex I, entry N'."
    )

    @field_validator("entity_type")
    @classmethod
    def validate_type(cls, v: str) -> str:
        allowed = {"person", "organisation"}
        if v.lower() not in allowed:
            raise ValueError(f"entity_type must be one of {allowed}, got '{v}'")
        return v.lower()


class SanctionMetadata(BaseModel):
    total_entities: int
    by_type: dict[str, int]


class SourceAOutput(BaseModel):
    """Root output schema for Source A."""
    source: str = "EUR-Lex EU 2019/796"
    extraction_date: str
    entities: list[SanctionEntity]
    metadata: SanctionMetadata


# ─────────────────────────────────────────────
# SOURCE B  —  Polish PEP Directory (rulers.org)
# ─────────────────────────────────────────────

ROLE_CATEGORIES = {"Head of State", "Prime Minister", "Minister", "Governor", "Senior Official", "Other"}

class PEPPerson(BaseModel):
    """
    A Politically Exposed Person from the Polish leaders directory on rulers.org.
    'current and recent' = serving at any point within the last 12 months.
    Polish diacritics must be preserved exactly (ł, ś, ź, ó, ą, ę, etc.).
    """
    name: str = Field(
        description="Full name of the person with all Polish diacritics preserved exactly. Do not transliterate."
    )
    role: str = Field(
        description="Top-level role category. Must be one of: 'Head of State', 'Prime Minister', 'Minister', 'Governor', 'Senior Official', 'Other'."
    )
    role_detail: Optional[str] = Field(
        default=None,
        description="The specific title or portfolio (e.g. 'Minister of Finance', 'President', 'Voivode of Masovian Voivodeship'). Null if not determinable."
    )
    start_date: Optional[str] = Field(
        default=None,
        description="Start date of this role. Use YYYY-MM-DD if full date known; YYYY-MM if only month/year; YYYY if only year. Null if unknown."
    )
    end_date: Optional[str] = Field(
        default=None,
        description="End date of this role. Null if the person is currently serving (no end date listed). Use same format as start_date."
    )
    currently_serving: bool = Field(
        description="True if no end date is listed or end date is in the future. False otherwise."
    )
    birth_year: Optional[str] = Field(
        default=None,
        description="Four-digit birth year if visible in the source (e.g. from '(b. 1969)'). Null if not listed."
    )
    notes: Optional[str] = Field(
        default=None,
        description="Any additional context: 'acting', '2nd time', party abbreviation, gender marker. Null if none."
    )
    source_reference: Optional[str] = Field(
        default=None,
        description="Hash or reference identifying the original source entry."
    )

    @field_validator("role")
    @classmethod
    def validate_role(cls, v: str) -> str:
        if v not in ROLE_CATEGORIES:
            raise ValueError(f"role must be one of {ROLE_CATEGORIES}, got '{v}'")
        return v


class PEPMetadata(BaseModel):
    total_persons: int
    by_role: dict[str, int]
    extraction_notes: str


class SourceBOutput(BaseModel):
    """Root output schema for Source B."""
    source: str = "rulers.org"
    country: str = "Poland"
    extraction_date: str
    persons: list[PEPPerson]
    metadata: PEPMetadata
