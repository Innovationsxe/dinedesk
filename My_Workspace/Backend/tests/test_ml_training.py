import unittest

from Backend.complaint_ml import CATEGORIES
from Backend.ml_training import predict_with_model, stratified_split, train_model


class ComplaintMlTrainingTests(unittest.TestCase):
    @staticmethod
    def sample_records():
        class_terms = (
            "velun",
            "martek",
            "sordip",
            "plazen",
            "frandol",
            "krevix",
            "womple",
        )
        records = []
        for category, term in zip(CATEGORIES, class_terms):
            for source in ("yelp", "local"):
                for index in range(10):
                    records.append(
                        {
                            "key": f"{source}:{category}:{index}",
                            "text": f"A {term} issue appeared in meal sample variant{index}.",
                            "category": category,
                            "source": source,
                        }
                    )
        return records

    def test_fixed_split_is_stratified_by_category_and_source(self):
        records = self.sample_records()

        training, validation, test = stratified_split(records)

        self.assertEqual(len(training) + len(validation) + len(test), len(records))
        for indices in (training, validation, test):
            split_groups = {
                (records[index]["category"], records[index]["source"])
                for index in indices
            }
            self.assertEqual(len(split_groups), len(CATEGORIES) * 2)

    def test_classical_candidates_train_and_produce_explanations(self):
        model = train_model(self.sample_records())
        result = predict_with_model(
            model,
            "A velun issue appeared in a fresh meal sample.",
        )

        self.assertEqual(result["category"], "Food quality")
        self.assertGreaterEqual(result["confidence"], 0.5)
        self.assertLessEqual(result["confidence"], 1.0)
        self.assertTrue(result["influential_words"])
        self.assertLessEqual(len(result["influential_words"]), 5)
        self.assertGreaterEqual(
            model["metrics"]["test"]["macro_f1"],
            model["metrics"]["keyword_baseline"]["macro_f1"],
        )


if __name__ == "__main__":
    unittest.main()
