"""Small, locked-down OpenAI Responses API client for advisory analysis only."""

from __future__ import annotations

import hashlib
import json
import os
import re
import ssl
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import HTTPSHandler, HTTPRedirectHandler, ProxyHandler, Request, build_opener

OPENAI_HOST = "https://api.openai.com"
RESPONSES_URL = OPENAI_HOST + "/v1/responses"
MAX_RESPONSE_BYTES = 1024 * 1024
MAX_SECRET_BYTES = 16 * 1024
_MODEL_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_VERDICTS = frozenset({"OPPORTUNITY", "WAIT", "AVOID", "INCONCLUSIVE"})
_ARRAY_FIELDS = ("bullish_evidence", "risk_flags", "invalidating_conditions", "data_quality_notes")

ASSESSMENT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "verdict": {"type": "string", "enum": sorted(_VERDICTS)},
        "confidence": {"type": "number", "minimum": 0, "maximum": 1},
        "summary": {"type": "string", "maxLength": 1000},
        "bullish_evidence": {"type": "array", "items": {"type": "string", "maxLength": 500}, "maxItems": 20},
        "risk_flags": {"type": "array", "items": {"type": "string", "maxLength": 500}, "maxItems": 20},
        "invalidating_conditions": {"type": "array", "items": {"type": "string", "maxLength": 500}, "maxItems": 20},
        "data_quality_notes": {"type": "array", "items": {"type": "string", "maxLength": 500}, "maxItems": 20},
    },
    "required": ["verdict", "confidence", "summary", *_ARRAY_FIELDS],
    "additionalProperties": False,
}


class AdvisorError(RuntimeError):
    """Safe-to-display failure without credentials or remote response bodies."""


class AdvisorConfigurationError(AdvisorError):
    pass


class AdvisorTransportError(AdvisorError):
    pass


class AdvisorResponseError(AdvisorError):
    pass


@dataclass(frozen=True)
class AdvisorResult:
    verdict: str
    confidence: float
    summary: str
    bullish_evidence: tuple[str, ...]
    risk_flags: tuple[str, ...]
    invalidating_conditions: tuple[str, ...]
    data_quality_notes: tuple[str, ...]
    model: str
    response_hash: str

    def as_json_dict(self) -> dict[str, Any]:
        return {
            "verdict": self.verdict,
            "confidence": self.confidence,
            "summary": self.summary,
            "bullish_evidence": list(self.bullish_evidence),
            "risk_flags": list(self.risk_flags),
            "invalidating_conditions": list(self.invalidating_conditions),
            "data_quality_notes": list(self.data_quality_notes),
            "model": self.model,
            "response_hash": self.response_hash,
        }


class _RejectRedirects(HTTPRedirectHandler):
    def redirect_request(self, req: Any, fp: Any, code: int, msg: str, headers: Any, newurl: str) -> None:
        raise AdvisorTransportError("OpenAI redirect rejected")


def read_secure_file(path: str | Path, *, label: str, maximum_bytes: int = MAX_SECRET_BYTES) -> str:
    """Read a small regular file without following a final-component symlink."""
    target = Path(path)
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(target, flags)
        try:
            stat = os.fstat(fd)
            if not __import__("stat").S_ISREG(stat.st_mode):
                raise AdvisorConfigurationError(f"{label} must be a regular non-symlink file")
            if stat.st_size <= 0 or stat.st_size > maximum_bytes:
                raise AdvisorConfigurationError(f"{label} file has invalid size")
            raw = os.read(fd, maximum_bytes + 1)
        finally:
            os.close(fd)
    except AdvisorConfigurationError:
        raise
    except OSError as exc:
        raise AdvisorConfigurationError(f"unable to read {label} file") from exc
    if len(raw) > maximum_bytes:
        raise AdvisorConfigurationError(f"{label} file is too large")
    try:
        value = raw.decode("utf-8").strip()
    except UnicodeDecodeError as exc:
        raise AdvisorConfigurationError(f"{label} file is not UTF-8") from exc
    if not value or "\x00" in value:
        raise AdvisorConfigurationError(f"{label} is empty or invalid")
    return value


def validate_model_id(model: str) -> str:
    model = model.strip()
    if not _MODEL_RE.fullmatch(model):
        raise AdvisorConfigurationError("invalid OpenAI model id")
    if model in {"gpt-5-codex", "codex-mini-latest"}:
        raise AdvisorConfigurationError("configured OpenAI Codex model is shut down")
    return model


def _validate_assessment(value: Any, model: str, response_hash: str) -> AdvisorResult:
    expected = {"verdict", "confidence", "summary", *_ARRAY_FIELDS}
    if not isinstance(value, dict) or set(value) != expected:
        raise AdvisorResponseError("OpenAI assessment has missing or extra fields")
    verdict = value["verdict"]
    confidence = value["confidence"]
    summary = value["summary"]
    if not isinstance(verdict, str) or verdict not in _VERDICTS:
        raise AdvisorResponseError("OpenAI assessment verdict is invalid")
    if isinstance(confidence, bool) or not isinstance(confidence, (int, float)) or not 0 <= confidence <= 1:
        raise AdvisorResponseError("OpenAI assessment confidence is invalid")
    if not isinstance(summary, str) or not summary.strip() or len(summary) > 1000:
        raise AdvisorResponseError("OpenAI assessment summary is invalid")
    arrays: dict[str, tuple[str, ...]] = {}
    for field in _ARRAY_FIELDS:
        items = value[field]
        if not isinstance(items, list) or len(items) > 20 or any(
            not isinstance(item, str) or not item.strip() or len(item) > 500 for item in items
        ):
            raise AdvisorResponseError(f"OpenAI assessment {field} is invalid")
        arrays[field] = tuple(items)
    return AdvisorResult(verdict, float(confidence), summary, arrays["bullish_evidence"], arrays["risk_flags"],
                         arrays["invalidating_conditions"], arrays["data_quality_notes"], model, response_hash)


def _extract_output_text(response: Any) -> str:
    if not isinstance(response, dict):
        raise AdvisorResponseError("OpenAI response is not an object")
    status = response.get("status")
    if status is not None and status != "completed":
        raise AdvisorResponseError("OpenAI response did not complete")
    if response.get("error") is not None:
        raise AdvisorResponseError("OpenAI response reported an error")
    if response.get("refusal"):
        raise AdvisorResponseError("OpenAI refused the assessment")
    direct = response.get("output_text")
    if isinstance(direct, str) and direct.strip():
        return direct
    chunks: list[str] = []
    output = response.get("output")
    if isinstance(output, list):
        for item in output:
            if not isinstance(item, dict):
                continue
            for content in item.get("content", []) if isinstance(item.get("content"), list) else []:
                if not isinstance(content, dict):
                    continue
                if content.get("type") == "refusal" or content.get("refusal"):
                    raise AdvisorResponseError("OpenAI refused the assessment")
                if content.get("type") == "output_text" and isinstance(content.get("text"), str):
                    chunks.append(content["text"])
    text = "".join(chunks)
    if not text.strip():
        raise AdvisorResponseError("OpenAI response contains no output_text")
    return text


class OpenAIAdvisor:
    """Advisory-only client. It exposes no tool, web-search, or order capability."""

    def __init__(self, api_key: str, model: str, *, opener: Any | None = None, timeout: float = 30.0) -> None:
        if not isinstance(api_key, str) or not api_key.strip() or len(api_key) > MAX_SECRET_BYTES:
            raise AdvisorConfigurationError("invalid OpenAI API key")
        self._api_key = api_key.strip()
        self.model = validate_model_id(model)
        self.timeout = timeout
        context = ssl.create_default_context()
        self._opener = opener or build_opener(ProxyHandler({}), _RejectRedirects(), HTTPSHandler(context=context))

    @classmethod
    def from_files(cls, api_key_file: str | Path, model_file: str | Path, **kwargs: Any) -> "OpenAIAdvisor":
        key = read_secure_file(api_key_file, label="OpenAI API key")
        model = validate_model_id(read_secure_file(model_file, label="OpenAI model", maximum_bytes=512))
        return cls(key, model, **kwargs)

    def assess(self, analysis_input: dict[str, Any]) -> AdvisorResult:
        canonical_input = json.dumps(analysis_input, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
        body = {
            "model": self.model,
            "instructions": (
                "Assess only the supplied immutable market observation. Be conservative. "
                "Do not alter or propose symbol, quantity, stop, target, or execution. Return only the schema."
            ),
            "input": canonical_input,
            "text": {"format": {"type": "json_schema", "name": "manual_market_assessment", "strict": True,
                                "schema": ASSESSMENT_SCHEMA}},
        }
        raw_body = json.dumps(body, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
        request = Request(RESPONSES_URL, data=raw_body, method="POST", headers={
            "Authorization": "Bearer " + self._api_key,
            "Content-Type": "application/json",
            "Accept": "application/json",
        })
        try:
            response = self._opener.open(request, timeout=self.timeout)
            try:
                status = getattr(response, "status", 200)
                if status != 200:
                    raise AdvisorTransportError(f"OpenAI request failed with HTTP {status}")
                raw = response.read(MAX_RESPONSE_BYTES + 1)
            finally:
                response.close()
        except AdvisorError:
            raise
        except HTTPError as exc:
            raise AdvisorTransportError(f"OpenAI request failed with HTTP {exc.code}") from None
        except (TimeoutError, URLError, OSError) as exc:
            raise AdvisorTransportError("OpenAI request timed out or failed") from exc
        if len(raw) > MAX_RESPONSE_BYTES:
            raise AdvisorResponseError("OpenAI response exceeded size limit")
        response_hash = hashlib.sha256(raw).hexdigest()
        try:
            envelope = json.loads(raw.decode("utf-8"))
            assessment = json.loads(_extract_output_text(envelope))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise AdvisorResponseError("OpenAI response is not valid structured JSON") from exc
        return _validate_assessment(assessment, self.model, response_hash)
