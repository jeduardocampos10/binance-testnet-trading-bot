from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from urllib.error import URLError

from app.openai_advisor import (
    MAX_RESPONSE_BYTES, AdvisorConfigurationError, AdvisorResponseError,
    AdvisorTransportError, OpenAIAdvisor,
)


VALID = {
    "verdict": "WAIT", "confidence": 0.7, "summary": "Wait for confirmation.",
    "bullish_evidence": ["trend"], "risk_flags": ["volatility"],
    "invalidating_conditions": ["close below support"], "data_quality_notes": [],
}


class FakeResponse:
    status = 200
    def __init__(self, raw: bytes): self.raw = raw
    def read(self, amount: int) -> bytes: return self.raw[:amount]
    def close(self) -> None: pass


class FakeOpener:
    def __init__(self, raw: bytes | None = None, error: Exception | None = None):
        self.raw, self.error, self.request, self.timeout = raw, error, None, None
    def open(self, request, timeout):
        self.request, self.timeout = request, timeout
        if self.error: raise self.error
        return FakeResponse(self.raw or b"")


def envelope(value=VALID) -> bytes:
    return json.dumps({"output": [{"content": [{"type": "output_text", "text": json.dumps(value)}]}]}).encode()


class OpenAIAdvisorTests(unittest.TestCase):
    def test_structured_request_and_valid_result(self) -> None:
        opener = FakeOpener(envelope())
        result = OpenAIAdvisor("secret", "gpt-test", opener=opener).assess({"symbol": "AAAUSDT"})
        self.assertEqual(result.verdict, "WAIT")
        self.assertEqual(opener.timeout, 30.0)
        self.assertEqual(opener.request.full_url, "https://api.openai.com/v1/responses")
        body = json.loads(opener.request.data)
        self.assertTrue(body["text"]["format"]["strict"])
        self.assertNotIn("tools", body)

    def test_rejects_extra_fields_refusal_timeout_and_oversize(self) -> None:
        invalid = dict(VALID, extra=True)
        with self.assertRaises(AdvisorResponseError):
            OpenAIAdvisor("secret", "gpt-test", opener=FakeOpener(envelope(invalid))).assess({})
        refusal = json.dumps({"output": [{"content": [{"type": "refusal", "refusal": "no"}]}]}).encode()
        with self.assertRaises(AdvisorResponseError):
            OpenAIAdvisor("secret", "gpt-test", opener=FakeOpener(refusal)).assess({})
        incomplete = json.dumps({"status": "incomplete", "output_text": json.dumps(VALID)}).encode()
        with self.assertRaises(AdvisorResponseError):
            OpenAIAdvisor("secret", "gpt-test", opener=FakeOpener(incomplete)).assess({})
        with self.assertRaises(AdvisorTransportError):
            OpenAIAdvisor("secret", "gpt-test", opener=FakeOpener(error=URLError("timeout"))).assess({})
        with self.assertRaises(AdvisorResponseError):
            OpenAIAdvisor("secret", "gpt-test", opener=FakeOpener(b"x" * (MAX_RESPONSE_BYTES + 1))).assess({})

    def test_secure_files_and_disabled_model(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); key = root / "key"; model = root / "model"
            key.write_text("secret-key", encoding="utf-8"); model.write_text("gpt-test", encoding="utf-8")
            advisor = OpenAIAdvisor.from_files(key, model, opener=FakeOpener(envelope()))
            self.assertEqual(advisor.model, "gpt-test")
            for disabled in ("gpt-5-codex", "codex-mini-latest"):
                model.write_text(disabled, encoding="utf-8")
                with self.assertRaises(AdvisorConfigurationError):
                    OpenAIAdvisor.from_files(key, model)
            link = root / "link"; link.symlink_to(key)
            with self.assertRaises(AdvisorConfigurationError): OpenAIAdvisor.from_files(link, model)


if __name__ == "__main__": unittest.main()
