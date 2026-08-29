"""Model provider — swappable LLM backends for the agents.

- `fake`: deterministic canned responses for tests and offline evals.
- `gemini`: Google Gemini via the Gemini API (AI Studio key).
- `vertex`: Gemini on Vertex AI (ADC / service account).

All paths produce ADK model objects usable as LlmAgent(model=...).
"""

from __future__ import annotations

import json
from typing import Any

from google.adk.models.base_llm import BaseLlm
from google.adk.models.llm_response import LlmResponse
from google.genai import types

from .compat import ensure_generate_content_response_compat

ensure_generate_content_response_compat()


class ScriptedLlm(BaseLm):
    """Yields scripted LlmResponses, selected by matching the prompt.

    Scripts are a list of (substring, response_text) pairs; the first
    substring found in the serialized prompt wins. Falls back to the last
    script entry. This is what the eval harness drives.
    """

    def __init__(self, scripts: list[tuple[str, str]]):
        super().__init__(model="scripted")
        self.scripts = scripts

    def _match(self, prompt_text: str) -> str:
        for needle, resp in self.scripts:
            if needle.lower() in prompt_text.lower():
                return resp
        return self.scripts[-1][1] if self.scripts else ""

    async def generate_content_async(self, llm_request, stream=False):
        prompt_parts = llm_request.contents or []
        serialized = " ".join(
            p.text or "" for c in prompt_parts for p in (c.parts or []) if p and p.text
        )
        serialized += " ".join(
            pc.text or "" for pc in (llm_request.system_instruction or []) if pc
        )
        text = self._match(serialized)
        part = types.Part(text=text)
        content = types.Content(role="model", parts=[part])
        yield LlmResponse(content=content, partial=False)


DEFAULT_DIAGNOSIS_SCRIPT = json.dumps(
    {
        "root_cause_class": "conveyor_jam",
        "summary": "Conveyor jam at dock-3: jam_seconds rising with throughput collapse; motor_overtemp log lines present.",
        "confidence": 0.86,
        "blast_radius": "dock-3 only; other docks nominal",
        "evidence_refs": ["metric:meridian_conveyor_jam_seconds", "log:motor_overtemp"],
        "recommended_action": "clear_jam",
    }
)

DEFAULT_PROPOSAL_SCRIPT = json.dumps(
    {
        "action": "clear_jam",
        "target": "dock-3",
        "params": {"dock": "dock-3"},
        "rationale": "Clearing the mechanical jam restores throughput; matches prior remediated incidents.",
        "risk": "low",
    }
)

DEFAULT_VERIFY_SCRIPT = json.dumps(
    {
        "verdict": "verified",
        "summary": "jam_seconds returned to 0 and throughput recovered to baseline within the verification window.",
        "evidence_refs": ["metric:meridian_conveyor_jam_seconds", "metric:meridian_sorter_throughput"],
        "confidence": 0.91,
    }
)

DEFAULT_SCRIPTS: list[tuple[str, str]] = [
    ("root_cause_class", DEFAULT_DIAGNOSIS_SCRIPT),
    ("recommended_action", DEFAULT_PROPOSAL_SCRIPT),
    ("verdict", DEFAULT_VERIFY_SCRIPT),
]


def build_model(provider: str | None = None, scripts: list[tuple[str, str]] | None = None):
    """Return an ADK model object for the configured provider."""
    from .config import SETTINGS

    provider = (provider or SETTINGS.ai_provider).lower()
    if provider == "fake":
        return ScriptedLlm(scripts or DEFAULT_SCRIPTS)
    if provider == "gemini":
        from google.adk.models.google_llm import Gemini

        return Gemini(model=SETTINGS.gemini_model, api_key=SETTINGS.gemini_api_key)
    if provider == "vertex":
        from google.adk.models.vertexai_llm import VertexAiLLm

        return VertexAiLLm(model=SETTINGS.gemini_model, project=SETTINGS.gcp_project)
    raise ValueError(f"unknown AI_PROVIDER {provider!r}")
