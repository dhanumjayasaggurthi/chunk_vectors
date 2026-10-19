import fitz
from backend.extract import extract_document,rule_extract,_verify_llm_field


def sample_pdf():
    pdf=fitz.open()
    p=pdf.new_page()
    p.insert_text((72,80),"Status: Approved, Date: 24 August 2022")
    p.insert_text((72,110),"STELARA (Ustekinumab)")
    p=pdf.new_page()
    p.insert_text((72,80),"The International Birth Date is 12 December 2008.")
    p.insert_text((72,110),"IBD refers to inflammatory bowel disease here.")
    content=pdf.tobytes();pdf.close()
    return content

class FakeLLM:
    def extract(self,pages,known):
        return {"fields":{},"summary":{"text":"Cumulative review of infant infections following in utero exposure to ustekinumab.","evidence_pages":[1,2]}},{}


def test_dates_and_products_have_page_evidence():
    fields,summary,status,note=extract_document(sample_pdf(),FakeLLM())
    assert status=="COMPLETED"
    assert fields["document_date"]["value"]=="2022-08-24"
    assert fields["international_birth_date"]["value"]=="2008-12-12"
    assert fields["international_birth_date"]["page"]==2
    assert fields["product"]["value"]=="STELARA"
    assert fields["active_ingredient"]["value"]=="Ustekinumab"
    assert "infections" in summary


def test_llm_cannot_create_unproven_field():
    pages=[{"page":1,"text":"IBD means inflammatory bowel disease. This page has no birth date."}]
    assert _verify_llm_field("international_birth_date",{
        "value":"2008-12-12", "quote":"International Birth Date: 12 December 2008", "page":1},pages) is None


def test_missing_summary_goes_to_review():
    fields,summary,status,note=extract_document(sample_pdf(),None)
    assert status=="REVIEW_REQUIRED" and "Summary unavailable" in note
