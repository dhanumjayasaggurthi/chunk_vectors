# `study_metadata_list_u.xlsx` — screenshot-derived source catalog

This file records only values visible in the user-supplied screenshot of `study_metadata_list_u.xlsx`. It is a source/provenance artifact for the MIR-AI implementation. It does **not** infer required/nullability semantics or reconstruct text clipped by the spreadsheet viewport.

## Visible schema

| # | Metadata Name | Data Type | Visible Example Value(s) |
|---:|---|---|---|
| 1 | Document ID | String | DOC-2024-001 |
| 2 | Document Title | String | 13-Week Rat Toxicity Study of JNJ-123456 |
| 3 | Batch / Lot number | String | 9822N, B240315A |
| 4 | Document Type | Categorical | Toxicology Report, Pharmacology Report, SOP |
| 5 | Study ID | String | TOX-2024-001, J93-0062-TX |
| 6 | Compound Number | String | JNJ-123456 |
| 7 | Compound Name | String | siRNA-ABC001 |
| 8 | Modality | Categorical | Small Molecule, siRNA, ASO, mAb, ADC |
| 9 | Therapeutic Area | Categorical | Oncology, Immunology, Neuroscience |
| 10 | Target | String | IL-23, EGFR, TNFα |
| 11 | Species | Categorical | Rat, Dog, Cynomolgus Monkey |
| 12 | Strain | String | C57BL/6; CD-1; Wistar Han; Beagle; Cynomolgus monkey |
| 13 | Sex | Categorical | Male, Female, Both |
| 14 | Age | Array (Numeric) | [1,2,4] |
| 15 | Age Unit | Categorical | weeks, days, years |
| 16 | CRO | String | Charles River, Labcorp, Inotiv |
| 17 | Study Type | Categorical | Repeat-Dose Toxicity, Safety Pharmacology, PK |
| 18 | Study Design | Text | Randomized repeat-dose toxicity study |
| 19 | Study Duration | String | 4 Weeks, 13 Weeks, 26 Weeks |
| 20 | Route of Administration | Categorical | Oral, IV, SC, IM |
| 21 | Dose Levels | Array (Numeric) | [10, 30, 100] mg/kg/day |
| 22 | Number of Animals | Integer | 80 |
| 23 | Recovery Group Included | Boolean | Yes |
| 24 | Organs Evaluated | Array (String) | [Liver, Kidney, Heart] |
| 25 | Target Organs Identified | Array (String) | [Liver, Kidney] |
| 26 | Findings by Organ | Array (Object) | Liver: Hypertrophy; Kidney: Tubular Degeneration |
| 27 | Adverse Findings | Array (String) | Hepatocellular necrosis |
| 28 | Adverse Events | Array (String) | Vomiting, Decreased Body Weight |
| 29 | Biomarkers Evaluated | Array (String) | ALT, AST, Troponin |
| 30 | Biomarker Changes | Text | ALT increased 3-fold at high dose |
| 31 | NOAEL | Numeric | 30 mg/kg/day |
| 32 | LOAEL | Numeric | 100 mg/kg/day |
| 33 | HNSTD | Numeric | 50 mg/kg/day |
| 34 | Exposure Margin | Numeric | 12x |
| 35 | Safety Margin | Numeric | 8x |
| 36 | Reversibility | Categorical | Fully Reversible |
| 37 | Mortality Observed | Boolean | Yes |
| 38 | Mortality Details | Text | 2 animals euthanized due to severe toxicity |
| 39 | Study Conclusion | Text | Compound generally well tolerated below 30 mg/kg/day |
| 40 | Toxicologist Interpretation | Text | Liver findings considered adaptive and non-adverse |
| 41 | Regulatory Relevance | Text | Supports FIH progression |
| 42 | Document Version | String | v1.0 |
| 43 | Report Date | Date | 5/15/2024 |
| 44 | Cmax | Numeric | 7190 ng/mL |
| 45 | AUC | Numeric | 32800 hr * ng/mL |
| 46 | Tmax | Numeric | 0.5 hr |
| 47 | Formulation | String | Formulated as an aqueous suspension containing … |
| 48 | vehicle type | String | Methylcellulose, PBS, Saline, Corn Oil |
| 49 | Vehicle concentration | String | 0.5% methylcellulose, 0.1% Tween 80 |
| 50 | Test Article Concentration | String | 20 mg/mL |

## Implementation constraints derived directly from this source

- The catalog contains **50 visible metadata fields**.
- Examples are illustrative; they are **not** treated as enumerated allowed values.
- Fields declared Numeric can visibly contain units in the spreadsheet examples. The ingestion implementation must retain the raw evidence and normalize numeric value/unit without silently discarding either.
- `Dose Levels` is an array of numeric values with a displayed unit.
- `Findings by Organ` is explicitly an array of objects.
- The source label `vehicle type` appears lower-case and is preserved as a source label; internal code may map it to a normalized key while retaining the original label for traceability.
- The screenshot does not state which fields are mandatory versus optional. That must come from the authoritative workbook/business rule, not inference.
- The Definitions column is not transcribed here because several cells are visually clipped; accuracy takes precedence over reconstructing hidden text.

## MIR-AI extraction rule

For each document, overlap analysis must compare these 50 requested metadata fields against authoritative RimDocs metadata. Fields already supplied by RimDocs must not be re-extracted by an LLM. Only missing requested fields are candidates for evidence-grounded LLM extraction. Raw source evidence, extraction status, normalization status, and provenance should be retained for auditability.
