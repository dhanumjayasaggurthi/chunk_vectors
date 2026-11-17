"""Business output contract: one CSV record per supported product/date pair."""
import csv
import re

COLUMNS = (
    "RIMdocs Object ID", "RIMdocs EDMS Number", "RIMdocs Product Code",
    "Product", "IBD", "DIBD", "Page Number", "Other Info",
)

def rows_for_document(document: dict):
    fields = document.get("fields") or {}
    case = fields.get("use_case")
    pairs = fields.get("birth_date_pairs") or []
    common = {
        "RIMdocs Object ID": document["r_object_id"],
        "RIMdocs EDMS Number": document.get("edms_number"),
        "RIMdocs Product Code": document.get("product_code"),
    }
    if not pairs:
        yield {**common, "Product": None, "IBD": None, "DIBD": None,
               "Page Number": None, "Other Info": None}
        return
    for pair in pairs:
        yield {**common, "Product": pair["product"],
               "IBD": pair["date"] if case == "IBD" else None,
               "DIBD": pair["date"] if case == "DIBD" else None,
               "Page Number": pair["page"], "Other Info": pair["other_info"]}

def safe_csv(row: dict) -> dict:
    # Spreadsheet formula injection mitigation for every source-derived text field.
    return {key: ("'" + str(value) if value is not None and
                  re.match(r"^\s*[=+\-@]", str(value)) else value)
            for key, value in row.items()}
