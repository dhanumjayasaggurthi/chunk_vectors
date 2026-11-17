"""Small synthetic PDF examples for local demonstration and regression.

These PDFs are *reconstructions* of slide examples. They are NOT original RIMdocs
documents, and must never be presented as evidence of corporate extraction accuracy.
"""
from __future__ import annotations

import textwrap
import fitz

SAMPLES = {
    "DEMO-RISP-001": {
        "name": "Risperidone",
        "case": "IBD",
        "edms_number": "DEMO-EDMS-RISP",
        "product_code": "DEMO-RISP",
        "body": """Periodic Benefit-Risk Evaluation Report
Risperidone
PERIOD COVERED BY THIS REPORT:
01 June 2011 to 31 May 2012
INTERNATIONAL BIRTH DATE:
01 December 1992
COUNTRY OF FIRST REGISTRATION:
United Kingdom""",
        "expected": [("Risperidone", "1992-12-01")],
        "evidence_page": 1,
    },
    "DEMO-CANA-002": {
        "name": "Canagliflozin",
        "case": "IBD",
        "edms_number": "DEMO-EDMS-CANA",
        "product_code": "DEMO-CANA",
        "body": """Periodic Benefit-Risk Evaluation Report
Canagliflozin and Canagliflozin/Metformin Hydrochloride
PERIOD COVERED BY THIS REPORT:
29 March 2017 to 28 March 2018
INTERNATIONAL BIRTH DATE:
29 March 2013: Canagliflozin""",
        "expected": [("Canagliflozin", "2013-03-29")],
        "evidence_page": 1,
    },
    "DEMO-PALI-003": {
        "name": "Paliperidone",
        "case": "IBD",
        "edms_number": "DEMO-EDMS-PALI",
        "product_code": "DEMO-PALI",
        "body": """Periodic Benefit-Risk Evaluation Report
Paliperidone and Paliperidone Palmitate
PERIOD COVERED BY THIS REPORT:
01 July 2025 to 30 June 2026
INTERNATIONAL BIRTH DATE:
19 December 2006: Paliperidone Extended Release
31 July 2009: Paliperidone Palmitate 1-Month Formulation
18 May 2015: Paliperidone Palmitate 3-Month Formulation
30 August 2021: Paliperidone Palmitate 6-Month Formulation""",
        "expected": [
            ("Paliperidone Extended Release", "2006-12-19"),
            ("Paliperidone Palmitate 1-Month Formulation", "2009-07-31"),
            ("Paliperidone Palmitate 3-Month Formulation", "2015-05-18"),
            ("Paliperidone Palmitate 6-Month Formulation", "2021-08-30"),
        ],
        "evidence_page": 1,
    },
    "DEMO-MACI-004": {
        "name": "macitentan/tadalafil",
        "case": "DIBD",
        "edms_number": "DEMO-EDMS-MACI",
        "product_code": "DEMO-MACI",
        "body": """EXECUTIVE SUMMARY
Introduction
This is the 11th DSUR for macitentan/tadalafil FDC, covering the reporting period
from 11 September 2025 to 13 October 2025.
The Development International Birth Date (DIBD) for macitentan/tadalafil FDC
is 11 September 2015. The International Birth Date (IBD) for the fixed dose
combination is 14 October 2021 based on the first marketing approval.""",
        "expected": [("macitentan/tadalafil", "2015-09-11")],
        "evidence_page": 6,
    },
    "DEMO-ERDA-005": {
        "name": "erdafitinib",
        "case": "DIBD",
        "edms_number": "DEMO-EDMS-ERDA",
        "product_code": "DEMO-ERDA",
        "body": """EXECUTIVE SUMMARY
Introduction
The 14th DSUR for JNJ-42756493, hereafter referred to as erdafitinib, is
covering the reporting period from 12 April 2025 to 11 April 2026.
The DIBD for erdafitinib is 4 June 2012.
The IBD for erdafitinib is 12 April 2019.""",
        "expected": [("erdafitinib", "2012-06-04")],
        "evidence_page": 6,
    },
}

def pdf_bytes(sample_id: str) -> bytes:
    sample = SAMPLES[sample_id]
    pdf = fitz.open()
    for n in range(1, sample["evidence_page"] + 1):
        page = pdf.new_page(width=595, height=842)
        if n != sample["evidence_page"]:
            page.insert_text((50, 50), f"Demo document {sample_id}, page {n}")
            continue
        text = sample["body"].splitlines()
        y = 55
        for line in text:
            if len(line) > 90:
                expanded = textwrap.wrap(line, width=85, break_long_words=False)
            else:
                expanded = [line]
            for part in expanded:
                page.insert_text((50, y), part, fontsize=10)
                y += 17
    raw = pdf.tobytes()
    pdf.close()
    return raw
