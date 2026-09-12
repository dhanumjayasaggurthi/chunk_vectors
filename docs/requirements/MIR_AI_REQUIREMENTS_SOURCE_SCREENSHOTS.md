Below is a **structured extraction using only text visible in the supplied images**. I have **not reconstructed cropped, hidden, or truncated text**. Where the screenshots themselves truncate a sample, I mark it as **[truncated in image]**.

# Requirement Documentation

## 1. Document Parsing and Extraction Expectations

### Parsing scope

- All documents will be parsed for:
  - Text
  - Tables
  - Charts/graphs
  - Images
- Scanned image documents are to be ingested with **OCR enabled**.
- File types in scope:
  - `.docx`
  - `.pdf`
- Images and tables are to be parsed and included with associated metadata, for example:
  - Bounding box
  - Page reference
  - Etc.

### Extraction expectations

- LLM-extracted metadata is to be acquired **only for fields present in** **`study_metadata_list_u.xlsx`** but **not present in RimDocs Metadata**.
- An **overlap analysis** must be performed between `study_metadata_list_u.xlsx` and RimDocs Metadata.
- Additional details are referenced in:
  - **Metadata Extraction and Propagation Requirements**

---

# 2. Table Handling and Representation Requirements

## Table extraction

- Extract tables **in their entirety**, including:
  - Table content
  - Generated summary
- Each table must be associated with:
  - Bounding box (**BBOX**) coordinates
  - Page label/number
  - Chunk metadata
- **Table and its summary must be included in a single chunk.**

## Sample of Vector Chunks with Metadata Fields

### Table Chunk Sample

The document states:

> **Table Chunk Sample: upgraded to match expected chunk structure**

> **Elastic Vector Chunk sample – Demonstrative, differs from actual source index**

### Sample document ID

```text
chunk-421f0c87488def5be626ad02a2ae9db2
```

### Visible content structure

```text
Table Analysis:
Image Path:
Caption: None
Structure: | Topic/Table/ Figure | Supporting Document a | Result Set ID b |
```

### Visible table rows

```text
Table 28, Table 29, Table 30, Table 31, Table 32, Table 33
Supporting documents:
JNJ-67896062-n002-00110
JNJ-67896062-n002-00131
Result Set IDs:
4345, 6133, 13079

Table 34, Table 35, Table 36, Table 37, Table 38, Table 39
Supporting documents:
JNJ-67896062-n002-00110
JNJ-67896062-n002-00150
Result Set IDs:
4345, 10027, 10562

Table 40, Table 41, Table 42, Table 43, Table 44
Supporting documents:
JNJ-67896062-n002-00110
JNJ-67896062-n002-00150
Result Set IDs:
4345, 9046, 9521

Table 46, Table 47, Table 48, Table 49, Table 50, Table 51
Supporting documents:
JNJ-67896062-n002-00110
JNJ-67896062-n002-00159
Result Set IDs:
4345, 11746, 12251

Table 52, Table 53, Table 54
Supporting documents:
JNJ-67896062-n002-00110
JNJ-67896062-n002-00150
Result Set IDs:
4345, 11099

Table 55, Table 56, Table 57
Supporting documents:
JNJ-67896062-n002-00110
JNJ-67896062-n002-00131
Result Set IDs:
4345, 7021

Table 58, Table 59, Table 60
Supporting documents:
JNJ-67896062-n002-00110
JNJ-67896062-n002-00174
Result Set IDs:
4345, 13708
```

**Note:** The screenshots visibly jump from Table 44 to Table 46. I have **not inserted Table 45**.

### Footnotes

```text
Footnotes: None
```

### Visible generated table analysis

The table is described as being organized into three columns:

- `Topic/Table/ Figure`
- `Supporting Document a`
- `Result Set ID b`

The visible generated analysis states that:

- The first column lists groups of tables by number, often in sequences of six, such as Table 28–33, or fewer, such as Table 52–54.
- The second column provides corresponding supporting-document identifiers beginning with:
  - `JNJ-67896062-n002-`
- The identifiers then have a unique numeric suffix.
- The third column lists Result Set IDs as numeric codes separated by commas.
- `JNJ-67896062-n002-00110` recurs across all visible rows and is described as a primary reference document.
- Other visible supporting document suffixes include:
  - `00131`
  - `00150`
  - `00159`
  - `00174`
- Visible Result Set IDs range from `4345` to `13708`.
- The analysis specifically states that `00150` is associated with table groups:
  - 34–39
  - 40–44
  - 52–54
- The generated analysis describes the relationships as supporting traceability and validation of experimental data within an **Electronic Laboratory Notebook (ELN)** framework.
- It refers to dissolution sciences data managed by:
  - `Janssen DPDS SM Dissolution Sciences, Higi Mumbai, India`
- It describes the table as illustrating systematic data management and referencing in pharmaceutical dissolution studies.

## Visible metadata from table chunk sample

```text
id:
chunk-421f0c87488def5be626ad02a2ae9db2

workspace_id:
scs_ii_release

created_at:
1783782175

page_label:
[42]

_node_type:
Node

document_id:
None

doc_id:
None

ref_doc_id:
None

report_title:
abc

study_id:
abc

artifact_name:
abc

compound_number:
JNJ-509877
```

### Other visible nested metadata values

```text
embedding: null

excluded_embed_metadata_keys: []

excluded_llm_metadata_keys: []

relationships: {}

metadata_template:
{key}: {value}

metadata_separator:
\n\n

image_resource:
null

audio_resource:
null

video_resource:
null

text_template:
{metadata_str}\n\n{content}

class_name:
Node
```

### Visible source file path

```text
30JUN2026/2026-06-30T0816 - truVAULT Production/TV-VAL-266585.pdf
```

### Visible embedding sample

```text
[0.0179443359375, -0.005527496337890625, -0.0187......]
```

The vector is visibly truncated in the image.

---

## Metadata Description for table chunks

### Metadata fields to include at chunk-level

```text
"id"
"created_at"
"content"
"full_doc_id"
"file_path"
"sourcefile_url"
"bbox"
"original_text"
"page_label"
"_node_content"
"_node_type"
"report_title"
"study_id"
"artifact_name"
"compound_number"
```

### Metadata generated by Pipeline

```text
"id"
"workspace_id"
"created_at"
"content"
"full_doc_id"
"file_path"
"bbox"
"original_text"
"page_label"
"_node_content"
"_node_type"
"document_id"
"doc_id"
"ref_doc_id"
```

### Metadata of low relevance that can be skipped

```text
"workspace_id"
"document_id"
"doc_id"
"ref_doc_id"
```

### Metadata appended using business-provided details / RimDocs Metadata

```text
"report_title"
"study_id"
"artifact_name"
"compound_number"
```

---

# 3. Chunking Strategy Expectations

## Approach

- **Split by headings first.**
- Preserve document hierarchy:
  - Title
  - Section
  - Subsection
- Do not chunk only by character count.
- Keep semantic units together.
- Avoid splitting:
  - Sentences
  - Paragraphs
  - Tables
  - Lists
  - Code blocks
- Remove headers and footers repeated on every page.
- Each chunk will include:
  - Page label or
  - Page number

## Parameters

```text
Chunk size: 1200–1500 token
Overlap: 100 token
Table and its summary in a single chunk
```

Additional visible requirement:

- Attach the following metadata with every chunk:
  - `doc_id`
  - Page number
  - Chunk ID
  - Table BBOX
  - Source URL

---

## Embedding & Vector Configuration

| ParameterValue    |                          |
| ----------------- | ------------------------ |
| Embedding Model   | `text-embedding-3-large` |
| API Version       | `2025-04-01-preview`     |
| Provider          | `Azure Open AI`          |
| Vector Dimensions | `3072`                   |
| Similarity Metric | `Cosine`                 |

---

## Sample of Vector Chunks with Metadata Fields — Text Chunk Sample

The document labels this:

> **Elastic Vector Chunk sample – Demonstrative, differs from actual source index**

### Document ID

```text
chunk-de1ce28e257b0817d4c956a1d3827bfe
```

### Visible content

```text
Text_Chunk Content Analysis:
Content: 181).

The ocular examinations were conducted using a hand-held
slit lamp and indirect
ophthalmoscope. ... ensuring data integrity
```

The sample itself states:

```text
(length truncated for cleaner representation)
```

### Visible metadata values

```text
id:
chunk-de1ce28e257b0817d4c956a1d3827bfe

workspace_id:
mirai_ingestion_lightrag_kg

created_at:
1782124747

full_doc_id:
doc-b6b9e2f43b80

file_path:
0901bacc8555d260.pdf

sourcefile_url:
http.abc

page_label:
[27, 28, 29, 30, 31, 32, 33, 34]

report_title:
A 26-week Repeated Dose Toxicity Study of JNJ-56021927-ZAH by Oral Gavage
Administration in Male Rats.

study_id:
tox103838

artifact_name:
Repeat Dose Toxicity Study Report

compound_number:
JNJ-56021927-ZAH

_node_type:
Node

document_id:
None

doc_id:
None

ref_doc_id:
None
```

### Visible embedding sample

```text
[-0.0028591156005859375, 0.005367279052734375, -0......]
```

The embedding is visibly truncated.

---

## Metadata Description for text chunk

### Metadata fields to include at Chunk-level

```text
"id"
"created_at"
"content"
"full_doc_id"
"file_path"
"sourcefile_url"
"page_label"
"_node_content"
"_node_type"
"report_title"
"study_id"
"artifact_name"
"compound_number"
```

### Metadata generated by Pipeline

```text
"id"
"workspace_id"
"created_at"
"content"
"full_doc_id"
"file_path"
"page_label"
"_node_content"
"_node_type"
"document_id"
"doc_id"
"ref_doc_id"
```

### Metadata details of low relevance which can be skipped

```text
"workspace_id"
"document_id"
"doc_id"
"ref_doc_id"
```

### Metadata appended using business-provided details from RimDocs metadata

```text
"report_title"
"study_id"
"artifact_name"
"compound_number"
```

### Image chunk sample

The screenshot contains the bullet/heading:

```text
Images chunk sample
```

No corresponding image-chunk sample content is visible in the supplied images.

---

# 4. Metadata Extraction and Propagation Requirement

The document says metadata will be referred to through **3 different categories**.

## 4.1 Business Supplied Metadata

Defined as:

> authoritative, document-level metadata provided by the business

Source:

```text
RimDocs Metadata
```

Requirements:

- **No extraction required** for this category.
- Ensure access to RimDocs Metadata for **all** **`69k`** **documents**.

---

## 4.2 LLM-Extracted Metadata

Defined as:

> document-level metadata extracted only for metadata fields not found in RimDocs metadata

Field list to consider:

```text
study_metadata_list_u.xlsx
```

### Extraction exercise

The extraction exercise is to be performed for the following sections of study reports:

- Summary
- Study administration
- Protocol
- Report approval sections
- First 20 pages if the mentioned sections are not found

### Document ID and relational database requirements

- Each document must have a **unique, system-generated** **`doc_id`**.
- Store and propagate metadata in a **Relational DB**.
- Use **system-generated document ID as the primary key**.
- Each unique system-generated Document ID will have all metadata fields as key-value pairs.

### Output format

- Extracted output is to be shared in requested **JSON format**.
- A result JSON sample is provided.

---

## Visible Result JSON Sample

Top-level key visible in the screenshot:

```text
0901bacc82f7bcd2
```

### Visible fields and values

```text
Document ID:
DOC-2024-001

Document Title:
13-Week Rat Toxicity Study of JNJ-123456

Batch / Lot number:
9822N

Document Type:
Toxicology Report

Study ID:
TOX-2024-001

Compound Number:
JNJ-123456

Compound Name:
siRNA-ABC001

Modality:
Small Molecule

Therapeutic Area:
Oncology

Target:
["IL-23", "EGFR", "TNFα"]

Species:
["Rat"]

Strain:
["C57BL/6", "CD-1", "Wistar Han"]

Sex:
["Male", "Female", "Both"]

Age:
[1, 2, 4]

Age Unit:
["weeks", "days", "years"]

CRO:
Charles River

Study Type:
Repeat-Dose Toxicity

Study Design:
Randomized repeat-dose toxicity study

Study Duration:
4 Weeks

Route of Administration:
Oral

Dose Levels:
[10, 30, 100 mg/kg/day]

Number of Animals:
80

Recovery Group Included:
Yes

Organs Evaluated:
["Liver", "Kidney", "Heart"]

Target Organs Identified:
["Liver", "Kidney"]

Findings by Organ:
[
  {"Liver": "Hypertrophy"},
  {"Kidney": "Tubular Degeneration"}
]

Adverse Findings:
["Hepatocellular necrosis"]

Adverse Events:
["Vomiting", "Decreased Body Weight"]

Biomarkers Evaluated:
["ALT", "AST", "Troponin"]

Biomarker Changes:
ALT increased 3-fold at high dose

NOAEL:
30
Displayed unit: mg/kg/day

LOAEL:
100

HNSTD:
50

Exposure Margin:
12

Safety Margin:
8

Reversibility:
Fully Reversible

Mortality Observed:
true

Mortality Details:
2 animals euthanized due to severe toxicity

Study Conclusion:
[
  "Compound generally well tolerated below 30 mg/kg/day",
  "Liver toxicity observed at highest dose",
  "13-week rat study showing dose-dependent liver effects"
]

Toxicologist Interpretation:
Liver findings considered adaptive and non-adverse at mid-dose

Regulatory Relevance:
Supports FIH progression

Document Version:
V1.0

Report Date:
5/15/2024

Cmax:
7190
Displayed unit: ng/mL

AUC:
32800
Displayed unit: hr * ng/mL

Tmax:
0.5

Formulation:
Formulated as an aqueous suspension containing 0.5% (w/v) hydroxypropyl
methylcellulose (HPMC) 2906 4000 mPa.s. in demineralized water target pH 5.0-9.0

vehicle type:
Methylcellulose

Vehicle concentration:
0.5% methylcellulose, 0.1% Tween 80

Test Article Concentration:
20 mg/mL
```

I preserved the capitalization visible in the screenshot, including lowercase `vehicle type`.

---

## 4.3 Chunk Provenance Metadata

Defined as:

> automatically generated retrieval metadata for every chunk, and details appended from business RimDocs metadata

Additional note:

- Elaborate details are shared in above sections of the document.

### Propagation requirements

- Ensure chunk metadata is available in the **vector database**.
- Ensure document-level metadata is populated in the **relational database**.
- `doc_id` must be:
  - System generated
  - The primary key
- For fields that match between sources:
  - The LLM extraction exercise can be skipped.

---

# 5. Expected Output Schema and Handover Format

The document states that expected output formats are shared in the preceding sections in elaborate detail.

## Handover process

### Deliverables

```text
All 69k processed documents
```

### Iterative handover

- **First review:** Sample dataset
- **Subsequent reviews:** Bundles of data
- **Final review:** All data

### Metadata synchronization requirement

Metadata extractions/appends should be in sync with:

```text
study_metadata_list_u.xlsx
```

---

# 6. Acceptance Criteria and Quality Expectations for Ingestion Artifacts

## General Criteria

- Documents parsed and chunked per the above strategy.
- Complete and accurate metadata, conforming to schema.
- Accurate table extraction:
  - BBOX
  - Summary
  - Content
- No splitting of semantic units.
- Headers/footers removed.
- Handover artifacts validated by **GRA team**.
- Iterative quality checks and review cycles.

## Benchmarks

```text
≥99% extraction accuracy for tables/metadata
≤1% error rate in chunking or metadata
```

Additional requirements:

- All mandatory fields populated.
- Missing/null values flagged.
- Golden set requirement is still open with Business.

## Review and Sign-off

```text
To be discussed
```

---

# 7. Additional Requirements for MirAI Ingestion and Benchmarking

- While the GRA data hub team is requesting all **69k documents and their metadata**, they can progressively perform the **Overlap Analysis** between:
  - `study_metadata_list_u.xlsx`
  - RimDocs metadata

## Multilingual Docs approach

- Where English counterparts exist, other-language documents will **not be ingested**.
- Mapping is to be provided by business.
- Discussion is open for non-English documents.
- Open question:
  - If yes, then who does translation?
  - Or should they be skipped?
- Open question:
  - Is there metadata to map?

---

# Open Question to Data Hub team

## Which vector DB will be utilized?

The document states:

- Need confirmation on:
  - **PG Vector**
  - **Elastic**
- If **PG Vector** is selected:
  - **ML retrieval pipeline to be upgraded**

---

# End of Document

```text
** END OF DOCUMENT **
```

## Consolidated key numbers/configuration

| ItemVisible value             |                                     |
| ----------------------------- | ----------------------------------- |
| Document population           | `69k`                               |
| Chunk size                    | `1200–1500 token`                   |
| Chunk overlap                 | `100 token`                         |
| Embedding model               | `text-embedding-3-large`            |
| API version                   | `2025-04-01-preview`                |
| Provider                      | `Azure Open AI`                     |
| Vector dimensions             | `3072`                              |
| Similarity metric             | `Cosine`                            |
| Extraction accuracy benchmark | `≥99%`                              |
| Error-rate benchmark          | `≤1%`                               |
| Candidate vector DBs          | `PG Vector`, `Elastic`              |
| Metadata field reference      | `study_metadata_list_u.xlsx`        |
| Business metadata source      | `RimDocs Metadata`                  |
| Document-level primary key    | System-generated `doc_id`           |
| Table requirement             | Table + summary in one chunk        |
| OCR                           | Enabled for scanned image documents |

No text outside what is visible in the screenshots has been added as a requirement or fact.
