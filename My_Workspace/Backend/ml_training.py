import math
import re
from collections import Counter

try:
    from sklearn.feature_extraction.text import TfidfVectorizer
    from sklearn.feature_selection import SelectPercentile, chi2
    from sklearn.metrics import f1_score, precision_recall_fscore_support
    from sklearn.model_selection import (
        GridSearchCV,
        StratifiedKFold,
        train_test_split,
    )
    from sklearn.naive_bayes import MultinomialNB
    from sklearn.pipeline import Pipeline
    from sklearn.svm import LinearSVC
    from sklearn.linear_model import LogisticRegression
    from sklearn.calibration import CalibratedClassifierCV
except ImportError:
    SKLEARN_AVAILABLE = False
else:
    SKLEARN_AVAILABLE = True

try:
    from .complaint_ml import CATEGORIES, redact_personal_details
except ImportError:
    from complaint_ml import CATEGORIES, redact_personal_details


RANDOM_STATE = 271828
TEST_SPLIT_KEY = "complaint-ml-fixed-test-v1"
MODEL_VERSION_KEY = "complaint-ml-current-version"
MODEL_TRAINED_AT_KEY = "complaint-ml-trained-at"
MINIMUM_TRAINING_EXAMPLES = 50
MINIMUM_PREDICTION_CONFIDENCE = 0.5
CATEGORY_ALIASES = {
    "wrong order": "Order accuracy",
    "missing item": "Order accuracy",
    "food quality": "Food quality",
    "food temperature": "Food quality",
    "long waiting time": "Service speed",
    "poor service": "Staff behaviour",
    "hygiene concern": "Hygiene and cleanliness",
    "incorrect bill": "Pricing or billing",
    "payment problem": "Pricing or billing",
}
KEYWORD_RULES = {
    "Food quality": {
        "stale", "spoiled", "rotten", "taste", "tasteless", "quality",
        "undercooked", "overcooked", "raw", "food poisoning",
    },
    "Order accuracy": {
        "wrong", "missing", "forgot", "incorrect order", "not received",
        "different order", "left out",
    },
    "Service speed": {
        "wait", "waiting", "late", "slow", "long time", "delay", "delayed",
    },
    "Staff behaviour": {
        "rude", "staff", "waiter", "waitress", "server", "manager",
        "unfriendly", "behaviour", "attitude",
    },
    "Hygiene and cleanliness": {
        "dirty", "hygiene", "clean", "unclean", "insect", "hair",
        "sanitary", "contamination",
    },
    "Pricing or billing": {
        "bill", "price", "charged", "charge", "payment", "refund",
        "overcharged", "incorrect amount",
    },
    "Other": set(),
}


def normalize_category(category):
    value = str(category or "").strip()
    canonical = next(
        (candidate for candidate in CATEGORIES if candidate.casefold() == value.casefold()),
        None,
    )
    if canonical:
        return canonical
    return CATEGORY_ALIASES.get(value.casefold(), "Other")


def parse_category_label(category):
    value = str(category or "").strip()
    canonical = next(
        (candidate for candidate in CATEGORIES if candidate.casefold() == value.casefold()),
        None,
    )
    return canonical or CATEGORY_ALIASES.get(value.casefold())


def stratified_split(records):
    if len(records) < MINIMUM_TRAINING_EXAMPLES:
        raise ValueError(
            f"At least {MINIMUM_TRAINING_EXAMPLES} labelled examples are required."
        )
    keys = [f"{record['category']}::{record['source']}" for record in records]
    counts = Counter(keys)
    if any(count < 3 for count in counts.values()):
        raise ValueError(
            "Each category/source group needs at least three examples to create "
            "the stratified train/validation/test split."
        )
    indices = list(range(len(records)))
    train_validation, test_indices = train_test_split(
        indices,
        test_size=0.15,
        random_state=RANDOM_STATE,
        stratify=keys,
    )
    train_keys = [keys[index] for index in train_validation]
    training_indices, validation_indices = train_test_split(
        train_validation,
        test_size=(0.15 / 0.85),
        random_state=RANDOM_STATE,
        stratify=train_keys,
    )
    return training_indices, validation_indices, test_indices


def _candidate_searches(training_texts, training_labels):
    cv = StratifiedKFold(n_splits=5, shuffle=True, random_state=RANDOM_STATE)
    specifications = [
        (
            "multinomial_naive_bayes",
            MultinomialNB(),
            {"classifier__alpha": [0.1, 0.5, 1.0, 2.0]},
        ),
        (
            "logistic_regression",
            LogisticRegression(
                class_weight="balanced",
                max_iter=2000,
                random_state=RANDOM_STATE,
            ),
            {"classifier__C": [0.1, 1.0, 10.0]},
        ),
        (
            "linear_svm",
            LinearSVC(class_weight="balanced", random_state=RANDOM_STATE),
            {"classifier__C": [0.1, 1.0, 10.0]},
        ),
    ]
    results = []
    for model_name, classifier, classifier_grid in specifications:
        pipeline = Pipeline(
            [
                (
                    "tfidf",
                    TfidfVectorizer(
                        ngram_range=(1, 2),
                        strip_accents="unicode",
                        sublinear_tf=True,
                        max_features=30000,
                    ),
                ),
                ("chi2", "passthrough"),
                ("classifier", classifier),
            ]
        )
        parameter_grid = [
            {**classifier_grid, "chi2": ["passthrough"]},
            {
                **classifier_grid,
                "chi2": [SelectPercentile(chi2, percentile=75)],
            },
        ]
        search = GridSearchCV(
            pipeline,
            parameter_grid,
            scoring="f1_macro",
            cv=cv,
            n_jobs=1,
            refit=True,
            error_score="raise",
        )
        fit_parameters = {}
        if model_name == "multinomial_naive_bayes":
            label_counts = Counter(training_labels)
            class_count = len(label_counts)
            fit_parameters["classifier__sample_weight"] = [
                len(training_labels) / (class_count * label_counts[label])
                for label in training_labels
            ]
        search.fit(training_texts, training_labels, **fit_parameters)
        results.append(
            {
                "name": model_name,
                "search": search,
                "cross_validation_macro_f1": float(search.best_score_),
                "parameters": {
                    key: (
                        "chi2_75_percentile"
                        if key == "chi2" and value != "passthrough"
                        else value
                        if isinstance(value, (str, int, float, bool, type(None)))
                        else value.__class__.__name__
                    )
                    for key, value in search.best_params_.items()
                },
            }
        )
    return results


def _metrics(labels, predictions, categories):
    per_class = precision_recall_fscore_support(
        labels,
        predictions,
        labels=categories,
        zero_division=0,
    )
    return {
        "macro_f1": round(float(f1_score(labels, predictions, average="macro", zero_division=0)), 4),
        "per_category": {
            category: {
                "precision": round(float(per_class[0][index]), 4),
                "recall": round(float(per_class[1][index]), 4),
                "f1": round(float(per_class[2][index]), 4),
                "support": int(per_class[3][index]),
            }
            for index, category in enumerate(categories)
            if per_class[3][index]
        },
        "examples": len(labels),
    }


def _keyword_prediction(text):
    words = set(re.findall(r"[a-z0-9]+", text.casefold()))
    text_lower = text.casefold()
    scores = {
        category: sum(
            1
            for keyword in keywords
            if " " not in keyword and keyword in words
        )
        + sum(1 for keyword in keywords if " " in keyword and keyword in text_lower)
        for category, keywords in KEYWORD_RULES.items()
    }
    return max(CATEGORIES, key=lambda category: (scores.get(category, 0), -CATEGORIES.index(category)))


def train_model(records, fixed_test_keys=None, existing_model=None):
    if not SKLEARN_AVAILABLE:
        raise RuntimeError(
            "scikit-learn is unavailable; complaint model training is disabled."
        )
    usable = [
        {
            **record,
            "text": redact_personal_details(record["text"]).strip(),
            "category": normalize_category(record["category"]),
        }
        for record in records
        if str(record.get("text", "")).strip()
    ]
    if len(usable) < MINIMUM_TRAINING_EXAMPLES:
        raise ValueError(
            f"At least {MINIMUM_TRAINING_EXAMPLES} labelled examples are required."
        )

    if fixed_test_keys:
        test_records = [record for record in usable if record["key"] in fixed_test_keys]
        development_records = [record for record in usable if record["key"] not in fixed_test_keys]
        if not test_records or not development_records:
            raise ValueError("The fixed evaluation set or training set is empty.")
        training_indices, validation_indices, _ = stratified_split(development_records)
        training_records = [development_records[index] for index in training_indices]
        validation_records = [development_records[index] for index in validation_indices]
        test_keys = sorted(record["key"] for record in test_records)
    else:
        training_indices, validation_indices, test_indices = stratified_split(usable)
        training_records = [usable[index] for index in training_indices]
        validation_records = [usable[index] for index in validation_indices]
        test_records = [usable[index] for index in test_indices]
        test_keys = sorted(record["key"] for record in test_records)

    training_labels = [record["category"] for record in training_records]
    if len(set(training_labels)) < 2:
        raise ValueError("Training data must contain at least two categories.")
    if min(Counter(training_labels).values()) < 5:
        raise ValueError(
            "Each category in the training split needs at least five examples "
            "for five-fold cross-validation."
        )
    searches = _candidate_searches(
        [record["text"] for record in training_records],
        training_labels,
    )

    candidates = []
    for item in searches:
        classifier = item["search"].best_estimator_
        validation_predictions = classifier.predict(
            [record["text"] for record in validation_records]
        )
        validation_labels = [record["category"] for record in validation_records]
        candidates.append(
            {
                **item,
                "estimator": classifier,
                "validation_macro_f1": float(
                    f1_score(
                        validation_labels,
                        validation_predictions,
                        average="macro",
                        zero_division=0,
                    )
                ),
            }
        )
    winner = max(
        candidates,
        key=lambda item: (
            item["validation_macro_f1"],
            item["cross_validation_macro_f1"],
        ),
    )

    development_records = training_records + validation_records
    estimator = winner["estimator"]
    if winner["name"] == "linear_svm":
        estimator = CalibratedClassifierCV(
            estimator=estimator,
            method="sigmoid",
            cv=3,
        )
    estimator.fit(
        [record["text"] for record in development_records],
        [record["category"] for record in development_records],
    )

    test_texts = [record["text"] for record in test_records]
    test_labels = [record["category"] for record in test_records]
    predictions = estimator.predict(test_texts)
    test_metrics = _metrics(test_labels, predictions, CATEGORIES)
    test_by_source = {
        source: _metrics(
            [
                record["category"]
                for record in test_records
                if record["source"] == source
            ],
            [
                prediction
                for record, prediction in zip(test_records, predictions)
                if record["source"] == source
            ],
            CATEGORIES,
        )
        for source in sorted({record["source"] for record in test_records})
    }
    majority = Counter(training_labels).most_common(1)[0][0]
    majority_metrics = _metrics(
        test_labels,
        [majority] * len(test_labels),
        CATEGORIES,
    )
    keyword_metrics = _metrics(
        test_labels,
        [_keyword_prediction(text) for text in test_texts],
        CATEGORIES,
    )
    if test_metrics["macro_f1"] <= keyword_metrics["macro_f1"]:
        raise ValueError(
            "The selected model did not beat the keyword-rule baseline on the "
            "held-out test set; it was not deployed."
        )

    if existing_model:
        current_test_predictions = existing_model["estimator"].predict(test_texts)
        current_test_metrics = _metrics(test_labels, current_test_predictions, CATEGORIES)
        if (
            test_metrics["macro_f1"]
            < current_test_metrics["macro_f1"] - 0.02
        ):
            raise ValueError(
                "The candidate model is more than 0.02 macro-F1 below the current "
                "model on the fixed test set; it was not deployed."
            )
    else:
        current_test_metrics = None

    metrics = {
        "selected_model": winner["name"],
        "selected_parameters": winner["parameters"],
        "cross_validation_macro_f1": round(
            winner["cross_validation_macro_f1"], 4
        ),
        "validation_macro_f1": round(winner["validation_macro_f1"], 4),
        "test": test_metrics,
        "test_by_source": test_by_source,
        "majority_baseline": majority_metrics,
        "keyword_baseline": keyword_metrics,
        "previous_model_test": current_test_metrics,
        "candidate_models": [
            {
                "name": item["name"],
                "cross_validation_macro_f1": round(item["cross_validation_macro_f1"], 4),
                "validation_macro_f1": round(item["validation_macro_f1"], 4),
                "parameters": item["parameters"],
            }
            for item in candidates
        ],
        "data": {
            "total_examples": len(usable),
            "training_examples": len(training_records),
            "validation_examples": len(validation_records),
            "test_examples": len(test_records),
            "sources": dict(Counter(record["source"] for record in usable)),
        },
    }
    return {
        "estimator": estimator,
        "metrics": metrics,
        "test_keys": test_keys,
        "categories": list(CATEGORIES),
    }


def predict_with_model(model, text):
    if not SKLEARN_AVAILABLE:
        raise RuntimeError(
            "scikit-learn is unavailable; complaint model predictions are disabled."
        )
    estimator = model["estimator"]
    cleaned = redact_personal_details(text)
    probabilities = estimator.predict_proba([cleaned])[0]
    classes = list(estimator.classes_)
    ranked = sorted(
        zip(classes, probabilities),
        key=lambda item: item[1],
        reverse=True,
    )
    category, confidence = ranked[0]
    needs_manual_review = bool(confidence < MINIMUM_PREDICTION_CONFIDENCE)
    if needs_manual_review:
        category = None
    return {
        "category": category,
        "confidence": round(float(confidence), 4),
        "needs_manual_review": needs_manual_review,
        "ranked_categories": [
            {"category": name, "confidence": round(float(score), 4)}
            for name, score in ranked[:3]
        ],
        "influential_words": influential_words(estimator, cleaned, category or ranked[0][0]),
    }


def _feature_names_and_coefficients(estimator, category):
    if isinstance(estimator, CalibratedClassifierCV):
        calibrated = estimator.calibrated_classifiers_[0]
        estimator = calibrated.estimator
    pipeline = estimator
    vectorizer = pipeline.named_steps["tfidf"]
    names = vectorizer.get_feature_names_out()
    selector = pipeline.named_steps.get("chi2")
    if selector != "passthrough":
        names = names[selector.get_support()]
    classifier = pipeline.named_steps["classifier"]
    classes = list(classifier.classes_)
    category_index = classes.index(category)
    coefficients = (
        classifier.feature_log_prob_
        if isinstance(classifier, MultinomialNB)
        else classifier.coef_
    )
    if isinstance(classifier, MultinomialNB):
        other_indices = [
            index for index in range(len(classes)) if index != category_index
        ]
        row = coefficients[category_index] - coefficients[other_indices].mean(axis=0)
    else:
        row = coefficients[category_index]
        if len(classes) == 2 and coefficients.shape[0] == 1:
            row = coefficients[0] if category_index == 1 else -coefficients[0]
    return names, row, vectorizer, selector


def influential_words(estimator, text, category, limit=5):
    names, coefficients, vectorizer, selector = _feature_names_and_coefficients(
        estimator, category
    )
    vector = vectorizer.transform([text])
    if selector != "passthrough":
        vector = selector.transform(vector)
    contributions = vector.toarray()[0] * coefficients
    top_indices = sorted(
        range(len(contributions)),
        key=lambda index: contributions[index],
        reverse=True,
    )
    return [
        {"word": str(names[index]), "contribution": round(float(contributions[index]), 5)}
        for index in top_indices
        if contributions[index] > 0
    ][:limit]


def wilson_interval(successes, sample_size, z=1.959963984540054):
    if sample_size < 30:
        return None
    proportion = successes / sample_size
    denominator = 1 + z * z / sample_size
    center = (proportion + z * z / (2 * sample_size)) / denominator
    margin = (
        z
        * math.sqrt(
            proportion * (1 - proportion) / sample_size
            + z * z / (4 * sample_size * sample_size)
        )
        / denominator
    )
    return [round(max(0.0, center - margin), 4), round(min(1.0, center + margin), 4)]


def weekly_spike_report(weekly_counts):
    report = []
    categories = sorted({category for week in weekly_counts for category in week})
    for category in categories:
        values = [int(week.get(category, 0)) for week in weekly_counts[-9:]]
        if len(values) < 9:
            values = [0] * (9 - len(values)) + values
        history = values[:8]
        current = values[8]
        mean = sum(history) / 8
        standard_deviation = math.sqrt(
            sum((count - mean) ** 2 for count in history) / 8
        )
        four_week_average = sum(values[-4:]) / 4
        x_values = range(4)
        recent = values[-4:]
        x_mean = 1.5
        y_mean = sum(recent) / 4
        slope = sum(
            (x - x_mean) * (y - y_mean) for x, y in zip(x_values, recent)
        ) / 5
        report.append(
            {
                "category": category,
                "current_week": current,
                "previous_eight_week_mean": round(mean, 2),
                "previous_eight_week_standard_deviation": round(standard_deviation, 2),
                "flagged": current >= 3 and current > mean + 2 * standard_deviation,
                "four_week_moving_average": round(four_week_average, 2),
                "four_week_trend_slope": round(slope, 2),
                "weekly_counts": values,
            }
        )
    return report


def smoothed_rating_weight(rating_sum, rating_count, prior_weight=3):
    if rating_count < 3:
        return 1.0
    smoothed_rating = (rating_sum + 3 * prior_weight) / (rating_count + prior_weight)
    return smoothed_rating / 5
