from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from tests.helpers import make_settings
from voicebot.ai import (
    GroqGateway,
    HybridGateway,
    OpenAIGateway,
    SummaryGenerationError,
    build_gateway,
)


def completion(payload):
    return SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content=json.dumps(payload)))]
    )


VALID_SUMMARY = {
    "summary": "A concise summary.",
    "key_points": ["One point"],
    "decisions": [],
    "action_items": [],
    "mentioned": [],
}


class FakeTranscriptions:
    def __init__(self, response="transcribed text"):
        self.response = response
        self.calls = []

    async def create(self, **kwargs):
        self.calls.append(kwargs)
        return self.response


class FakeCompletions:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    async def create(self, **kwargs):
        self.calls.append(kwargs)
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


class FakeClient:
    def __init__(self, summary_responses, transcript_response="transcribed text"):
        self.audio = SimpleNamespace(transcriptions=FakeTranscriptions(transcript_response))
        self.chat = SimpleNamespace(completions=FakeCompletions(summary_responses))


class GatewayTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temporary = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.settings = make_settings(Path(self.temporary.name))
        self.audio = Path(self.temporary.name) / "audio.ogg"
        self.audio.write_bytes(b"OggS")

    async def asyncTearDown(self):
        try:
            self.temporary.cleanup()
        except Exception:
            pass

    async def test_strict_schema_primary_success(self):
        client = FakeClient([completion(VALID_SUMMARY)])
        gateway = GroqGateway(self.settings, client)
        summary = await gateway.summarize("hello")
        self.assertEqual(summary.summary, "A concise summary.")
        call = client.chat.completions.calls[0]
        self.assertEqual(call["model"], self.settings.summary_model)
        self.assertTrue(call["response_format"]["json_schema"]["strict"])
        self.assertEqual(call["reasoning_effort"], "low")
        self.assertEqual(len(client.chat.completions.calls), 1)

    async def test_invalid_primary_uses_fallback(self):
        invalid = SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content="not json"))]
        )
        client = FakeClient([invalid, completion(VALID_SUMMARY)])
        gateway = GroqGateway(self.settings, client)
        summary = await gateway.summarize("hello")
        self.assertEqual(summary.summary, "A concise summary.")
        self.assertEqual(
            client.chat.completions.calls[1]["model"], self.settings.summary_fallback_model
        )

    async def test_both_models_fail_with_stable_error(self):
        client = FakeClient([RuntimeError("one"), RuntimeError("two")])
        gateway = GroqGateway(self.settings, client)
        with self.assertRaises(SummaryGenerationError):
            await gateway.summarize("hello")

    async def test_transcription_string_and_empty_response(self):
        client = FakeClient([], transcript_response=" text ")
        gateway = GroqGateway(self.settings, client)
        self.assertEqual(await gateway.transcribe(self.audio), "text")
        call = client.audio.transcriptions.calls[0]
        self.assertEqual(call["model"], self.settings.transcription_model)
        self.assertEqual(call["response_format"], "text")

        empty = GroqGateway(self.settings, FakeClient([], transcript_response="   "))
        with self.assertRaisesRegex(ValueError, "no speech"):
            await empty.transcribe(self.audio)

    async def test_openai_gateway_uses_configured_models(self):
        settings = make_settings(
            Path(self.temporary.name),
            STT_PROVIDER="openai",
            SUMMARY_PROVIDER="openai",
        )
        client = FakeClient([completion(VALID_SUMMARY)])
        gateway = OpenAIGateway(settings, client)
        summary = await gateway.summarize("hello")
        self.assertEqual(summary.summary, "A concise summary.")
        self.assertEqual(client.chat.completions.calls[0]["model"], settings.summary_model)
        self.assertNotIn("reasoning_effort", client.chat.completions.calls[0])

    async def test_hybrid_gateway_routes_stt_and_summary_independently(self):
        settings = make_settings(
            Path(self.temporary.name),
            STT_PROVIDER="groq",
            SUMMARY_PROVIDER="openai",
        )
        stt_client = FakeClient([], transcript_response=" hybrid text ")
        summary_client = FakeClient([completion(VALID_SUMMARY)])
        gateway = HybridGateway(settings, stt_client, summary_client)
        self.assertEqual(gateway.stt_provider, "groq")
        self.assertEqual(gateway.summary_provider, "openai")
        self.assertEqual(await gateway.transcribe(self.audio), "hybrid text")
        self.assertEqual((await gateway.summarize("hi")).summary, "A concise summary.")

    async def test_build_gateway_factory(self):
        settings = make_settings(Path(self.temporary.name), SUMMARY_PROVIDER="openai")
        gateway = build_gateway(
            settings,
            stt_client=FakeClient([]),
            summary_client=FakeClient([completion(VALID_SUMMARY)]),
        )
        self.assertIsInstance(gateway, HybridGateway)


if __name__ == "__main__":
    unittest.main()
