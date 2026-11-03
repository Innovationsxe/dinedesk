import unittest

from Backend.complaint_ml import (
    classify_complaint,
    draft_with_gemini,
    redact_personal_details,
    retrieve_solutions,
)
from Backend.ml_training import weekly_spike_report, wilson_interval


class ComplaintMlTests(unittest.TestCase):
    def test_redacts_identifiers_before_text_is_used(self):
        redacted = redact_personal_details(
            "Call Ada Guest at 071 234 5678 or ada@example.com, 10 Main Street. "
            "The manager Alice reviewed it.",
            ("Ada Guest",),
        )

        self.assertNotIn("Ada Guest", redacted)
        self.assertNotIn("071 234 5678", redacted)
        self.assertNotIn("ada@example.com", redacted)
        self.assertNotIn("10 Main Street", redacted)
        self.assertNotIn("Alice", redacted)

    def test_classifies_using_similar_labelled_complaints(self):
        examples = [
            {
                "text": "The sushi fish tasted stale and the rice was sour",
                "category": "Food quality",
            },
            {
                "text": "The salmon tasted old and smelled unpleasant",
                "category": "Food quality",
            },
            {
                "text": "We received the wrong roll and one item was missing",
                "category": "Wrong order",
            },
        ]

        result = classify_complaint("The fish smelled stale and tasted old", examples)

        self.assertEqual(result["category"], "Food quality")
        self.assertGreater(result["confidence"], 0)
        self.assertEqual(result["training_examples"], 3)

    def test_does_not_claim_a_classification_without_enough_examples(self):
        result = classify_complaint(
            "A completely unrelated concern",
            [{"text": "The soup was cold", "category": "Food temperature"}],
        )

        self.assertIsNone(result["category"])
        self.assertEqual(result["training_examples"], 1)

    def test_retrieved_solutions_are_redacted(self):
        result = retrieve_solutions(
            "The sushi arrived cold and unpleasant",
            [
                {
                    "text": "Ada Guest said the sushi arrived cold",
                    "solution": "We are sorry, Ada Guest. We will review it.",
                    "category": "Food temperature",
                }
            ],
            known_details=("Ada Guest",),
        )

        self.assertEqual(len(result), 1)
        self.assertNotIn("Ada Guest", result[0]["complaint"])
        self.assertNotIn("Ada Guest", result[0]["solution"])

    def test_solution_retrieval_uses_category_and_smoothed_ratings(self):
        examples = [
            {
                "text": "The order arrived cold",
                "solution": "We will review this issue.",
                "category": "Food quality",
                "rating_sum": 15,
                "rating_count": 3,
            },
            {
                "text": "The order arrived cold",
                "solution": "We will review the delivery process.",
                "category": "Order accuracy",
            },
        ]

        results = retrieve_solutions(
            "The order arrived cold",
            examples,
            predicted_category="Food quality",
        )

        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["category"], "Food quality")
        self.assertEqual(results[0]["ranking_score"], results[0]["similarity"] * 0.8)

    def test_weekly_spike_report_flags_only_a_large_current_week(self):
        weekly_counts = [{"Food quality": 0} for _ in range(8)]
        weekly_counts.append({"Food quality": 3})

        report = weekly_spike_report(weekly_counts)[0]

        self.assertTrue(report["flagged"])
        self.assertEqual(report["current_week"], 3)
        self.assertEqual(report["four_week_moving_average"], 0.75)

    def test_wilson_interval_is_hidden_until_thirty_cases(self):
        self.assertIsNone(wilson_interval(20, 29))
        interval = wilson_interval(20, 30)
        self.assertEqual(len(interval), 2)
        self.assertLess(interval[0], 20 / 30)
        self.assertGreater(interval[1], 20 / 30)

    def test_gemini_is_not_used_without_configuration(self):
        with self.assertRaisesRegex(ValueError, "not configured"):
            draft_with_gemini("The order was late.", [], "")


if __name__ == "__main__":
    unittest.main()
