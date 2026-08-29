"""Compatibility shim for the pinned google-genai release.

google-adk 2.7.x reads several top-level attributes off
``GenerateContentResponse`` (``partial``, ``content``, ...) in telemetry and
the workflow node plumbing. The pinned google-genai wheel does not declare
all of them. We declare every missing attribute as an optional field with a
falsy default; payloads that never carry these keys are unaffected.
"""

from __future__ import annotations

from pydantic.fields import FieldInfo

from google.genai import types

_REQUIRED_ATTRS = (
    "cache_metadata",
    "citation_metadata",
    "content",
    "environment_id",
    "error_code",
    "error_message",
    "finish_reason",
    "go_away",
    "grounding_metadata",
    "input_transcription",
    "interrupted",
    "live_session_resumption_update",
    "model_version",
    "output_transcription",
    "partial",
    "turn_complete",
    "voice_activity",
)

_applied = False


def ensure_generate_content_response_compat() -> None:
    global _applied
    if _applied:
        return
    fields = types.GenerateContentResponse.model_fields
    missing = [name for name in _REQUIRED_ATTRS if name not in fields]
    if missing:
        for name in missing:
            fields[name] = FieldInfo(default=None, annotation=object | None)
        types.GenerateContentResponse.model_rebuild(force=True)
    _applied = True
