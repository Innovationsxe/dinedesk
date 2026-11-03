import argparse
import csv
import hashlib
from datetime import datetime, timezone
from pathlib import Path

from app import COMPLAINT_CATEGORIES, DATABASE_ERRORS, get_connection
from complaint_ml import redact_personal_details
from ml_training import parse_category_label


TEXT_COLUMNS = ("complaint_text", "text", "review_text")


def import_training_file(file_path, source):
    imported = 0
    skipped = 0
    with Path(file_path).open("r", encoding="utf-8-sig", newline="") as source_file:
        reader = csv.DictReader(source_file)
        if not reader.fieldnames:
            raise ValueError("The CSV must include a header row.")
        text_column = next(
            (column for column in TEXT_COLUMNS if column in reader.fieldnames),
            None,
        )
        has_adjudicated_column = bool(
            {"category", "adjudicated_category"} & set(reader.fieldnames)
        )
        has_two_annotators = {
            "annotator_one_category",
            "annotator_two_category",
        }.issubset(reader.fieldnames)
        if not text_column or not (has_adjudicated_column or has_two_annotators):
            raise ValueError(
                "The CSV must include a complaint_text (or text/review_text) "
                "column and a category or two annotator-category columns."
            )
        if source == "yelp" and "stars" not in reader.fieldnames:
            raise ValueError("Yelp CSV files must include a stars column.")

        with get_connection() as connection:
            identity_rows = connection.execute(
                "SELECT customer_name, customer_email FROM complaints"
            ).fetchall()
            known_details = []
            for row in identity_rows:
                known_details.extend(
                    value
                    for value in (row["customer_name"], row["customer_email"])
                    if value and value != "Not provided"
                )

            for row_number, row in enumerate(reader, 2):
                if source == "yelp":
                    try:
                        stars = float(row.get("stars", ""))
                    except (TypeError, ValueError) as error:
                        raise ValueError(
                            f"Row {row_number} has an invalid stars value."
                        ) from error
                    if stars > 2:
                        skipped += 1
                        continue

                category_value = str(
                    row.get("adjudicated_category")
                    or row.get("category")
                    or ""
                ).strip()
                category = parse_category_label(category_value)
                text = redact_personal_details(
                    row.get(text_column, ""),
                    known_details,
                ).strip()
                annotator_one = (
                    parse_category_label(row["annotator_one_category"])
                    if str(row.get("annotator_one_category") or "").strip()
                    else None
                )
                annotator_two = (
                    parse_category_label(row["annotator_two_category"])
                    if str(row.get("annotator_two_category") or "").strip()
                    else None
                )
                if (
                    str(row.get("annotator_one_category") or "").strip()
                    and annotator_one is None
                    or str(row.get("annotator_two_category") or "").strip()
                    and annotator_two is None
                ):
                    raise ValueError(
                        f"Row {row_number} contains an unsupported annotator label."
                    )
                if (
                    annotator_one
                    and annotator_two
                    and annotator_one != annotator_two
                    and not str(row.get("adjudicated_category") or "").strip()
                ):
                    raise ValueError(
                        f"Row {row_number} has disagreeing annotations but no "
                        "adjudicated_category."
                    )
                if (
                    not category_value
                    and annotator_one
                    and annotator_one == annotator_two
                ):
                    category = annotator_one
                if not text or category is None:
                    raise ValueError(
                        f"Row {row_number} needs non-empty text and an adjudicated "
                        "category label."
                    )

                text_hash = hashlib.sha256(
                    f"{source}\0{category}\0{text.casefold()}".encode("utf-8")
                ).hexdigest()
                cursor = connection.execute(
                    """
                    INSERT OR IGNORE INTO complaint_training_examples
                        (text, category, source, text_hash, created_at,
                         annotator_one_category, annotator_two_category)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        text,
                        category,
                        source,
                        text_hash,
                        datetime.now(timezone.utc).isoformat(),
                        annotator_one,
                        annotator_two,
                    ),
                )
                imported += cursor.rowcount

    return {"imported": imported, "skipped": skipped}


def main():
    parser = argparse.ArgumentParser(
        description=(
            "Import locally stored, labelled complaint examples. Yelp examples "
            "are filtered to reviews with at most two stars."
        )
    )
    parser.add_argument("csv_file", type=Path, help="Path to a local CSV file")
    parser.add_argument(
        "--source",
        choices=("yelp", "local"),
        required=True,
        help="Dataset source for provenance tracking",
    )
    arguments = parser.parse_args()

    try:
        result = import_training_file(arguments.csv_file, arguments.source)
    except (OSError, ValueError) as error:
        parser.error(str(error))
    except DATABASE_ERRORS as error:
        parser.error(str(error))
    print(
        f"Imported {result['imported']} labelled examples; skipped "
        f"{result['skipped']} records (including non-negative Yelp reviews)."
    )


if __name__ == "__main__":
    main()
