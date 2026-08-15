import unittest
from types import SimpleNamespace
from unittest.mock import patch

from services.ai_contract import (
    WORKOUT_ANALYSIS_PROMPT,
    WorkoutAnalysis,
)
from services.anthropic_service import analyze_workout as analyze_anthropic_workout
from services.mistral_service import analyze_workout as analyze_mistral_workout
from services.openai_service import analyze_workout as analyze_openai_workout


API_KEY = "provider-user-key-1234567890-Ab12"
EMAIL = "user@example.com"
SAMPLE_ANALYSIS = {
    "title": "Squats and rowing",
    "summary": "A strength session followed by a steady row.",
    "duration_minutes": 45,
    "exercises": [
        {
            "name": "Goblet squat",
            "sets": 3,
            "reps": "10 reps",
            "weight": "20 kg",
            "duration": None,
            "distance": None,
            "notes": None,
        }
    ],
    "intensity": "moderate",
    "confidence": "high",
    "assumptions": [],
    "needs_clarification": False,
    "clarification_question": "",
}


class WorkoutProviderTests(unittest.TestCase):
    @patch("services.openai_service.OpenAI")
    def test_openai_uses_structured_workout_contract(self, openai_mock):
        parsed = WorkoutAnalysis.model_validate(SAMPLE_ANALYSIS)
        openai_mock.return_value.responses.parse.return_value.output_parsed = parsed

        result = analyze_openai_workout(
            "  Three sets of goblet squats  ",
            EMAIL,
            API_KEY,
            "gpt-5.6-sol",
        )

        self.assertEqual(result["exercises"][0]["name"], "Goblet squat")
        request = openai_mock.return_value.responses.parse.call_args.kwargs
        self.assertEqual(request["input"][0]["content"], WORKOUT_ANALYSIS_PROMPT)
        self.assertEqual(request["input"][1]["content"], "Three sets of goblet squats")
        self.assertIs(request["text_format"], WorkoutAnalysis)
        self.assertFalse(request["store"])

    @patch("services.anthropic_service.Anthropic")
    def test_anthropic_uses_structured_workout_contract(self, anthropic_mock):
        parsed = WorkoutAnalysis.model_validate(SAMPLE_ANALYSIS)
        client = anthropic_mock.return_value.__enter__.return_value
        client.messages.parse.return_value = SimpleNamespace(
            parsed_output=parsed,
            stop_reason="end_turn",
        )

        result = analyze_anthropic_workout(
            "Three sets of goblet squats",
            EMAIL,
            API_KEY,
            "claude-sonnet-5",
        )

        self.assertEqual(result["duration_minutes"], 45)
        request = client.messages.parse.call_args.kwargs
        self.assertEqual(request["system"], WORKOUT_ANALYSIS_PROMPT)
        self.assertIs(request["output_format"], WorkoutAnalysis)

    @patch("services.mistral_service.Mistral")
    def test_mistral_uses_structured_workout_contract(self, mistral_mock):
        parsed = WorkoutAnalysis.model_validate(SAMPLE_ANALYSIS)
        client = mistral_mock.return_value.__enter__.return_value
        client.chat.parse.return_value = SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(parsed=parsed))]
        )

        result = analyze_mistral_workout(
            "Three sets of goblet squats",
            EMAIL,
            API_KEY,
            "mistral-small-2603",
        )

        self.assertEqual(result["confidence"], "high")
        request = client.chat.parse.call_args.kwargs
        self.assertEqual(request["messages"][0]["content"], WORKOUT_ANALYSIS_PROMPT)
        self.assertIs(request["response_format"], WorkoutAnalysis)
        self.assertEqual(request["temperature"], 0)


if __name__ == "__main__":
    unittest.main()
