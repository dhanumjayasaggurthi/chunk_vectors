"""Regulatory extraction regression fixtures derived from the supplied examples."""
import pytest
from backend.birth_dates import case_for_artifact, extract_pairs, result_fields
from backend.birth_date_export import rows_for_document, safe_csv

def page(text, number=1):
    return {"page":number, "text":text}

def test_pbrer_multiple_formulations_and_dates():
    text = """Periodic Benefit-Risk Evaluation Report
Paliperidone and Paliperidone Palmitate
PERIOD COVERED BY THIS REPORT:
01 July 2025 to 30 June 2026
INTERNATIONAL BIRTH DATE:
19 December 2006: Paliperidone Extended Release
31 July 2009: Paliperidone Palmitate 1-Month Formulation
18 May 2015: Paliperidone Palmitate 3-Month Formulation
30 August 2021: Paliperidone Palmitate 6-Month Formulation
"""
    rows, errors = extract_pairs([page(text)], "IBD")
    assert errors == []
    assert [(r["product"], r["date"]) for r in rows] == [
        ("Paliperidone Extended Release", "2006-12-19"),
        ("Paliperidone Palmitate 1-Month Formulation", "2009-07-31"),
        ("Paliperidone Palmitate 3-Month Formulation", "2015-05-18"),
        ("Paliperidone Palmitate 6-Month Formulation", "2021-08-30"),
    ]
    assert all(r["page"] == 1 for r in rows)
    assert rows[0]["other_info"]

def test_single_product_cover_and_ibd():
    text = """Periodic Benefit Risk Evaluation Report
Risperidone
PERIOD COVERED BY THIS REPORT:
INTERNATIONAL BIRTH DATE:
01 December 1992
COUNTRY OF FIRST REGISTRATION:
United Kingdom
"""
    pairs, errors = extract_pairs([page(text)], "IBD")
    assert not errors
    assert pairs[0]["product"] == "Risperidone"
    assert pairs[0]["date"] == "1992-12-01"

def test_scan_stops_after_25_pages_and_not_found():
    pages = [page("No birth date here", n) for n in range(1, 26)]
    pages.append(page("International Birth Date: 01 December 1992", 26))
    assert extract_pairs(pages, "IBD")[0] == []

def test_dsur_dibd_not_ibd():
    pages = [page("""EXECUTIVE SUMMARY
Introduction
The 11th DSUR for macitentan/tadalafil FDC is covered here.
The Development International Birth Date (DIBD) for macitentan/tadalafil FDC is 11 September 2015.
The International Birth Date (IBD) for macitentan/tadalafil FDC is 14 October 2021.
""", 6)]
    pairs, errors = extract_pairs(pages, "DIBD")
    assert not errors
    assert len(pairs) == 1
    assert pairs[0]["date"] == "2015-09-11"
    assert pairs[0]["product"] == "macitentan/tadalafil"

def test_erdafitinib_dibd():
    pairs, errors = extract_pairs([page(
        "EXECUTIVE SUMMARY\nThe DIBD for erdafitinib is 4 June 2012. "
        "The IBD for erdafitinib is 12 April 2019.", 6)], "DIBD")
    assert not errors
    assert pairs[0]["date"] == "2012-06-04"

def test_ambiguous_cover_never_assigns_wrong_product():
    pairs, issues = extract_pairs([page(
        "Periodic Benefit Risk Evaluation Report\nA and B\n"
        "INTERNATIONAL BIRTH DATE:\n04 April 1973\n")], "IBD")
    assert not pairs and issues

def test_pair_conflicts_require_review():
    pairs, issues = extract_pairs([page(
        "INTERNATIONAL BIRTH DATE:\n01 December 1992: Risperidone\n"
        "02 December 1992: Risperidone\n")], "IBD")
    assert len(pairs) == 2
    assert any("Conflicting" in problem for problem in issues)

def test_export_one_record_per_pair_with_identifiers():
    pairs, _ = extract_pairs([page("INTERNATIONAL BIRTH DATE:\n"
        "19 December 2006: Paliperidone Extended Release\n"
        "31 July 2009: Paliperidone Palmitate 1-Month Formulation")], "IBD")
    doc = {"r_object_id":"object-1", "edms_number":"EDMS-RIM-123",
           "product_code":"CODE-9", "fields":result_fields("IBD", pairs)}
    rows = list(rows_for_document(doc))
    assert len(rows) == 2
    assert rows[0]["RIMdocs EDMS Number"] == "EDMS-RIM-123"
    assert rows[1]["Product"] == "Paliperidone Palmitate 1-Month Formulation"
    assert rows[1]["IBD"] == "2009-07-31"

def test_csv_formula_injection():
    assert safe_csv({"Product":"=HYPERLINK(\"evil\")"})["Product"].startswith("'")

@pytest.mark.parametrize(("artifact","case"), [
    ("Periodic Benefit Risk Evaluation Report (PBRER)","IBD"),
    ("Development Safety Update Report (DSUR)","DIBD"),
    ("Clinical Trial Document",None),
])
def test_artifact_dispatch(artifact,case):
    assert case_for_artifact(artifact) == case


def test_pbrer_never_treats_development_date_as_ibd():
    pairs, issues = extract_pairs([page(
        "The Development International Birth Date (DIBD) is 11 September 2015.\n"
        "INTERNATIONAL BIRTH DATE:\n"
        "19 December 2006: Paliperidone Extended Release\n")], "IBD")
    assert not issues
    assert pairs[0]["date"] == "2006-12-19"


def test_dsur_keeps_section_one_when_section_two_starts_on_same_page():
    pairs, issues = extract_pairs([page(
        "EXECUTIVE SUMMARY\n"
        "The DIBD for erdafitinib is 4 June 2012.\n"
        "2. WORLDWIDE MARKETING AUTHORIZATION STATUS\n"
        "The DIBD for another medicine is 14 October 2021.", 6)], "DIBD")
    assert not issues
    assert len(pairs) == 1
    assert pairs[0]["product"] == "erdafitinib"
    assert pairs[0]["date"] == "2012-06-04"
