#!/usr/bin/env python3
"""
LLM extraction engine for the PST.AG assessment.

This module keeps extraction declarative by letting the Pydantic models define
what we want while the runtime handles provider orchestration, retries, and
JSON normalization.

Turbo-V2 additions:
  - Groq model tiering (primary -> fallback model)
  - 2-second spacing between successful requests
  - Exponential backoff for 429s
  - Rate-limit header awareness for proactive throttling
  - Groq rotation first, Gemini fallback second
"""

from __future__ import annotations

import json
import os
import re
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import requests

sys.path.insert(0, str(Path(__file__).parent.parent))
from models import PEPPerson, SanctionEntity


GROQ_MODELS = [
    os.getenv("GROQ_PRIMARY_MODEL", "llama-3.3-70b-versatile"),
    os.getenv("GROQ_FALLBACK_MODEL", "llama-3.1-8b-instant"),
]
GEMINI_MODELS = [
    os.getenv("GEMINI_MODEL", "gemini-1.5-flash"),
]

MIN_SUCCESS_INTERVAL_SECONDS = float(os.getenv("LLM_MIN_SUCCESS_INTERVAL_SECONDS", "2"))
BASE_BACKOFF_SECONDS = float(os.getenv("LLM_BASE_BACKOFF_SECONDS", "2"))
MAX_BACKOFF_SECONDS = float(os.getenv("LLM_MAX_BACKOFF_SECONDS", "60"))
REQUEST_TIMEOUT_SECONDS = int(os.getenv("LLM_REQUEST_TIMEOUT_SECONDS", "30"))


def build_field_lines_from_model(model_cls, prefix: str = "") -> str:
    """Introspects a Pydantic model to recursively build prompt field semantics."""
    lines = []
    for name, info in model_cls.model_fields.items():
        annotation = info.annotation
        # If the annotation is a nested Pydantic model, recurse
        if hasattr(annotation, "model_fields"):
            lines.append(build_field_lines_from_model(annotation, prefix=f"{prefix}{name}."))
        # If the annotation is Optional[NestedModel], recurse into the inner type
        elif hasattr(annotation, "__args__") and any(hasattr(a, "model_fields") for a in annotation.__args__):
            inner_type = next(a for a in annotation.__args__ if hasattr(a, "model_fields"))
            lines.append(build_field_lines_from_model(inner_type, prefix=f"{prefix}{name}."))
        else:
            if info.description:
                lines.append(f"  - {prefix}{name}: {info.description}")
    return "\n".join(lines)


@dataclass
class ProviderState:
    name: str
    blocked_until: float = 0.0
    consecutive_rate_limits: dict[str, int] = field(default_factory=dict)


_LAST_SUCCESS_AT = 0.0
_PROVIDER_STATES = {
    "groq": ProviderState(name="groq"),
    "gemini": ProviderState(name="gemini"),
}


def _log(message: str) -> None:
    print(f"  [LLM] {message}")


def _sleep_for_spacing() -> None:
    global _LAST_SUCCESS_AT
    if _LAST_SUCCESS_AT <= 0:
        return
    wait_time = (_LAST_SUCCESS_AT + MIN_SUCCESS_INTERVAL_SECONDS) - time.time()
    if wait_time > 0:
        time.sleep(wait_time)


def _mark_success() -> None:
    global _LAST_SUCCESS_AT
    _LAST_SUCCESS_AT = time.time()


def _safe_float(raw: Optional[str]) -> Optional[float]:
    if raw is None:
        return None
    text = str(raw).strip().lower()
    if not text:
        return None
    text = text.replace("seconds", "s").replace("second", "s")
    match = re.match(r"^(\d+(?:\.\d+)?)(ms|s|m|h)?$", text)
    if not match:
        return None
    value = float(match.group(1))
    unit = match.group(2) or "s"
    if unit == "ms":
        return value / 1000.0
    if unit == "m":
        return value * 60.0
    if unit == "h":
        return value * 3600.0
    return value


def _rate_limit_delay_from_headers(headers: requests.structures.CaseInsensitiveDict) -> Optional[float]:
    candidates = [
        headers.get("retry-after"),
        headers.get("x-ratelimit-reset-requests"),
        headers.get("x-ratelimit-reset-tokens"),
        headers.get("ratelimit-reset"),
    ]
    delays = [_safe_float(value) for value in candidates]
    delays = [delay for delay in delays if delay is not None and delay >= 0]
    return max(delays) if delays else None


def _remaining_from_headers(
    headers: requests.structures.CaseInsensitiveDict,
) -> tuple[Optional[int], Optional[int]]:
    request_remaining = headers.get("x-ratelimit-remaining-requests")
    token_remaining = headers.get("x-ratelimit-remaining-tokens")
    try:
        req_val = int(request_remaining) if request_remaining is not None else None
    except ValueError:
        req_val = None
    try:
        tok_val = int(token_remaining) if token_remaining is not None else None
    except ValueError:
        tok_val = None
    return req_val, tok_val


def _apply_provider_cooldown(provider: str, delay_seconds: float, reason: str) -> None:
    state = _PROVIDER_STATES[provider]
    bounded_delay = min(delay_seconds, MAX_BACKOFF_SECONDS)
    state.blocked_until = max(state.blocked_until, time.time() + bounded_delay)
    _log(f"{provider} cooling down for {bounded_delay:.1f}s ({reason})")


def _wait_if_provider_blocked(provider: str) -> None:
    blocked_until = _PROVIDER_STATES[provider].blocked_until
    wait_time = blocked_until - time.time()
    if wait_time > 0:
        _log(f"{provider} blocked for another {wait_time:.1f}s")
        time.sleep(wait_time)


def _record_rate_limit(provider: str, limiter_key: str, headers: requests.structures.CaseInsensitiveDict) -> None:
    state = _PROVIDER_STATES[provider]
    strikes = state.consecutive_rate_limits.get(limiter_key, 0) + 1
    state.consecutive_rate_limits[limiter_key] = strikes
    delay = _rate_limit_delay_from_headers(headers)
    if delay is None:
        delay = min(BASE_BACKOFF_SECONDS * (2 ** (strikes - 1)), MAX_BACKOFF_SECONDS)
    _apply_provider_cooldown(provider, delay, f"429 on {limiter_key}")


def _record_success(provider: str, limiter_key: str, headers: requests.structures.CaseInsensitiveDict) -> None:
    state = _PROVIDER_STATES[provider]
    state.consecutive_rate_limits[limiter_key] = 0

    request_remaining, token_remaining = _remaining_from_headers(headers)
    reset_delay = _rate_limit_delay_from_headers(headers)
    if reset_delay is not None and (
        (request_remaining is not None and request_remaining <= 1)
        or (token_remaining is not None and token_remaining <= 0)
    ):
        _apply_provider_cooldown(provider, reset_delay, "approaching rate limit")


def load_api_keys() -> list[str]:
    """
    Load all available API keys (Groq and Gemini) into a flat list.
    Deduplicates while preserving order.
    """
    env_path = Path(__file__).with_name(".env")
    if env_path.exists():
        for line in env_path.read_text(encoding="utf-8").splitlines():
            stripped = line.strip()
            if not stripped or stripped.startswith("#") or "=" not in stripped:
                continue
            key, value = stripped.split("=", 1)
            os.environ.setdefault(key.strip(), value.strip().strip("\"'"))

    def parse_keys(env_var: str) -> list[str]:
        raw = os.getenv(env_var, "")
        return [k.strip() for k in raw.split(",") if k.strip()]

    all_keys: list[str] = []
    all_keys.extend(parse_keys("GROQ_API_KEYS"))
    if os.getenv("GROQ_API_KEY"):
        all_keys.append(os.getenv("GROQ_API_KEY").strip())
    all_keys.extend(parse_keys("GEMINI_API_KEYS"))
    if os.getenv("GEMINI_API_KEY"):
        all_keys.append(os.getenv("GEMINI_API_KEY").strip())
    return list(dict.fromkeys(all_keys))


def _call_groq(prompt: str, api_key: str, model: str) -> str:
    _wait_if_provider_blocked("groq")
    _sleep_for_spacing()

    response = requests.post(
        "https://api.groq.com/openai/v1/chat/completions",
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        json={
            "model": model,
            "messages": [
                {
                    "role": "system",
                    "content": "You are a JSON-only extraction engine. Always return purely valid JSON.",
                },
                {"role": "user", "content": prompt},
            ],
            "response_format": {"type": "json_object"},
            "temperature": 0.0,
        },
        timeout=REQUEST_TIMEOUT_SECONDS,
    )

    limiter_key = f"{api_key[:8]}:{model}"
    if response.status_code == 429:
        _record_rate_limit("groq", limiter_key, response.headers)
        raise RuntimeError(f"Groq rate limited for model {model}")

    response.raise_for_status()
    _record_success("groq", limiter_key, response.headers)
    _mark_success()
    payload = response.json()
    return payload["choices"][0]["message"]["content"]


def call_google_gemini(prompt: str, api_key: str, model: Optional[str] = None) -> str:
    """
    Call Google Gemini via REST API, trying v1 first and v1beta second.
    """
    _wait_if_provider_blocked("gemini")
    _sleep_for_spacing()

    model_name = model or GEMINI_MODELS[0]
    headers = {"Content-Type": "application/json"}
    payload = {
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {"response_mime_type": "application/json"},
    }

    last_error: Optional[Exception] = None
    limiter_key = api_key[:8]
    for api_version in ("v1", "v1beta"):
        url = f"https://generativelanguage.googleapis.com/{api_version}/models/{model_name}:generateContent?key={api_key}"
        response = requests.post(url, headers=headers, json=payload, timeout=REQUEST_TIMEOUT_SECONDS)
        if response.status_code == 404:
            last_error = RuntimeError(f"Gemini endpoint {api_version} not available for model {model_name}")
            continue
        if response.status_code == 429:
            _record_rate_limit("gemini", limiter_key, response.headers)
            raise RuntimeError("Gemini rate limited")
        try:
            response.raise_for_status()
            _record_success("gemini", limiter_key, response.headers)
            _mark_success()
            data = response.json()
            return data["candidates"][0]["content"]["parts"][0]["text"]
        except Exception as exc:
            last_error = exc
            continue

    if last_error is not None:
        raise last_error
    raise RuntimeError("Gemini request failed without a detailed error")


def call_gemini_with_rotation(prompt: str, keys: list[str]) -> str:
    """
    Shared provider orchestrator.

    Rotation order:
      1. Every Groq key on the primary model
      2. Every Groq key on the fallback model
      3. Every Gemini key
    """
    groq_keys = [key for key in keys if key.startswith("gsk_")]
    gemini_keys = [key for key in keys if key.startswith("AIza")]

    if not groq_keys and not gemini_keys:
        raise RuntimeError("No API keys found. Configure GROQ_API_KEYS / GEMINI_API_KEYS.")

    while True:
        if groq_keys:
            for model in list(dict.fromkeys(GROQ_MODELS)):
                for key in groq_keys:
                    try:
                        if model != GROQ_MODELS[0]:
                            _log(f"trying Groq fallback tier {model} on key {key[:8]}...")
                        return _call_groq(prompt, key, model)
                    except Exception as exc:
                        _log(f"Groq {model} on key {key[:8]}... failed: {exc}")
                        continue

        if gemini_keys:
            _log("Groq unavailable or exhausted. Falling back to Gemini...")
            for key in gemini_keys:
                try:
                    return call_google_gemini(prompt, key)
                except Exception as exc:
                    _log(f"Gemini key {key[:8]}... failed: {exc}")
                    continue

        next_available = min(state.blocked_until for state in _PROVIDER_STATES.values())
        wait_time = max(next_available - time.time(), BASE_BACKOFF_SECONDS)
        _log(f"all providers exhausted, waiting {wait_time:.1f}s before retry")
        time.sleep(min(wait_time, MAX_BACKOFF_SECONDS))


def build_source_a_prompt(entry_text: str, entry_idx: int) -> str:
    """
    Build a declarative extraction prompt for a Source A (EUR-Lex) entry.
    """
    field_lines = build_field_lines_from_model(SanctionEntity)
    return (
        "You are a compliance data extractor. Extract structured data from "
        "this EU sanctions regulation Annex I entry.\n\n"
        "Return ONLY a valid JSON object with these fields (use null for missing values):\n"
        "{\n"
        '  "entity_type": string,\n'
        '  "name": string,\n'
        '  "aliases": [string],\n'
        '  "identifiers": {\n'
        '    "date_of_birth": string|null,\n'
        '    "place_of_birth": string|null,\n'
        '    "nationality": string|null,\n'
        '    "passport_number": string|null,\n'
        '    "national_id": string|null,\n'
        '    "address": string|null,\n'
        '    "gender": string|null\n'
        "  },\n"
        '  "listing_reason": string|null,\n'
        '  "date_listed": string|null,\n'
        f'  "source_reference": "Annex I, entry {entry_idx}"\n'
        "}\n\n"
        "Field semantics (use these descriptions to locate the right value):\n"
        f"{field_lines}\n\n"
        f"Entry text:\n{entry_text.strip()}\n\n"
        "Return only the JSON object. No markdown, no explanation."
    )


def build_source_b_prompt(entry_text: str, section_context: str) -> str:
    """
    Build a declarative extraction prompt for a Source B (rulers.org) entry.
    """
    field_lines = build_field_lines_from_model(PEPPerson)
    return (
        "You are a precision data extractor specializing in Politically Exposed Persons (PEP). "
        "Extract structured data from this Polish government official entry.\n\n"
        f"Section context (where this entry appears): {section_context}\n\n"
        "Return ONLY a valid JSON object with these fields (use null for missing values):\n"
        "{\n"
        '  "name": string,\n'
        '  "role": string,\n'
        '  "role_detail": string|null,\n'
        '  "start_date": string|null,\n'
        '  "end_date": string|null,\n'
        '  "currently_serving": boolean,\n'
        '  "birth_year": string|null,\n'
        '  "notes": string|null\n'
        "}\n\n"
        "Field semantics:\n"
        f"{field_lines}\n\n"
        f"Entry text:\n{entry_text.strip()}\n\n"
        "Return only the JSON object. No markdown, no explanation."
    )


def parse_json_output(output: str) -> dict:
    """
    Parse LLM JSON output with light error recovery.
    """
    output = re.sub(r"```(?:json)?\s*", "", output).strip()
    match = re.search(r"\{.*\}", output, re.DOTALL)
    if not match:
        raise ValueError(f"No JSON object found in LLM output: {output[:200]}")
    try:
        return json.loads(match.group(0))
    except json.JSONDecodeError as exc:
        raise ValueError(f"Invalid JSON in LLM output: {exc}\nRaw: {match.group(0)[:300]}") from exc


def validate_source_a_entity(record: dict) -> SanctionEntity:
    if "identifiers" not in record:
        record["identifiers"] = {}
    if "entity_type" in record:
        record["entity_type"] = str(record["entity_type"]).lower().strip()
    try:
        return SanctionEntity(**record)
    except Exception as exc:
        raise ValueError(f"Source A schema validation failed: {exc}") from exc


def validate_source_b_person(record: dict) -> PEPPerson:
    allowed_roles = {"Head of State", "Prime Minister", "Minister", "Governor", "Senior Official", "Other"}
    if record.get("role") not in allowed_roles:
        record["role"] = "Other"
    if not isinstance(record.get("currently_serving"), bool):
        record["currently_serving"] = record.get("end_date") is None
    try:
        return PEPPerson(**record)
    except Exception as exc:
        raise ValueError(f"Source B schema validation failed: {exc}") from exc


def extract_source_a_entry(
    entry_text: str,
    entry_idx: int,
    keys: list[str],
) -> Optional[SanctionEntity]:
    """
    Declarative extraction of one EUR-Lex Annex I entry via LLM.
    """
    prompt = build_source_a_prompt(entry_text, entry_idx)
    print(f"  [LLM-A] Processing Entry {entry_idx}...")
    raw = call_gemini_with_rotation(prompt, keys)
    record = parse_json_output(raw)
    entity = validate_source_a_entity(record)
    safe_name = entity.name.encode("ascii", "replace").decode("ascii")
    print(f"  [LLM-A] -> {entity.entity_type}: {safe_name}")
    return entity


def extract_source_b_entry(
    entry_text: str,
    section_context: str,
    keys: list[str],
) -> Optional[PEPPerson]:
    """
    Declarative extraction of one rulers.org PEP entry via LLM.
    """
    prompt = build_source_b_prompt(entry_text, section_context)
    print(f"  [LLM-B] Processing entry in {section_context[:30]}...")
    raw = call_gemini_with_rotation(prompt, keys)
    record = parse_json_output(raw)
    person = validate_source_b_person(record)
    safe_name = person.name.encode("ascii", "replace").decode("ascii")
    print(f"  [LLM-B] -> {person.role}: {safe_name}")
    return person


def load_gemini_api_key() -> str:
    """Legacy single-key loader - returns first available key."""
    return load_api_keys()[0]


def build_prompt(html_entry: str) -> str:
    """Legacy: delegates to Source A prompt builder."""
    return build_source_a_prompt(html_entry, entry_idx=0)


def call_gemini(prompt: str, api_key: str) -> str:
    """Legacy: delegates to rotation caller with a single key."""
    return call_gemini_with_rotation(prompt, [api_key])


def parse_json_output_legacy(output: str) -> dict:
    return parse_json_output(output)


def validate_entity(record: dict) -> SanctionEntity:
    return validate_source_a_entity(record)


parse_json_output = parse_json_output
validate_entity = validate_entity


if __name__ == "__main__":
    print("Testing API key rotation...")
    keys = load_api_keys()
    print(f"Found {len(keys)} key(s)")
    test_prompt = 'Return this exact JSON: {"status": "ok"}'
    try:
        result = call_gemini_with_rotation(test_prompt, keys)
        print(f"API call succeeded: {result[:100]}")
    except Exception as exc:
        print(f"API call failed: {exc}")
