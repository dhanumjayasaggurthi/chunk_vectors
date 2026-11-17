"""Offline original-PDF upload regression checks; no corporate credentials."""
import fitz
from fastapi.testclient import TestClient
from backend import config
from backend.app import app
from backend.demo_documents import pdf_bytes
from backend.local_pdf import inspect_pdf_bytes,LocalPDFError

def test_original_pdf_source_text_to_four_product_rows():
    result=inspect_pdf_bytes(pdf_bytes("DEMO-PALI-003"),"paliperidone.pdf","IBD")
    assert result["status"]=="EVIDENCE_FOUND"
    assert len(result["pairs"])==4
    assert [p["date"] for p in result["pairs"]]==["2006-12-19","2009-07-31","2015-05-18","2021-08-30"]
    assert result["metadata_verified"] is False

def test_dsur_dibd_distinguished_from_ibd():
    result=inspect_pdf_bytes(pdf_bytes("DEMO-MACI-004"),"macitentan.pdf","DIBD")
    assert result["pairs"][0]["date"]=="2015-09-11"
    assert result["pairs"][0]["page"]==6

def test_missing_product_does_not_guess():
    pdf=fitz.open()
    page=pdf.new_page()
    page.insert_text((40,50),"INTERNATIONAL BIRTH DATE:")
    page.insert_text((40,80),"01 December 1992")
    raw=pdf.tobytes();pdf.close()
    result=inspect_pdf_bytes(raw,"unknown.pdf","IBD")
    assert result["status"]=="REVIEW_REQUIRED"
    assert not result["pairs"]

def test_invalid_pdf_rejected():
    try:inspect_pdf_bytes(b"not pdf","bad.pdf","IBD")
    except LocalPDFError:pass
    else:assert False

def test_local_upload_api_and_demo_block(monkeypatch):
    monkeypatch.setattr(config,"MODE","local")
    with TestClient(app) as client:
        resp=client.post("/api/v1/local/inspect",data={"use_case":"DIBD"},
             files={"file":("source.pdf",pdf_bytes("DEMO-ERDA-005"),"application/pdf")})
        assert resp.status_code==200,resp.text
        assert resp.json()["pairs"][0]["date"]=="2012-06-04"
        assert resp.json()["metadata_verified"] is False
        bad=client.post("/api/v1/local/inspect",data={"use_case":"IBD"},
             files={"file":("source.pdf",b"not pdf","application/pdf")})
        assert bad.status_code==422
    monkeypatch.setattr(config,"MODE","demo")
    with TestClient(app) as client:
        resp=client.post("/api/v1/local/inspect",data={"use_case":"IBD"},
             files={"file":("source.pdf",pdf_bytes("DEMO-RISP-001"),"application/pdf")})
        assert resp.status_code==404

def test_local_upload_denies_cross_origin(monkeypatch):
    monkeypatch.setattr(config,"MODE","local")
    with TestClient(app) as client:
        response=client.post("/api/v1/local/inspect",
            headers={"Origin":"https://untrusted.example"},
            data={"use_case":"IBD"},
            files={"file":("source.pdf",pdf_bytes("DEMO-RISP-001"),"application/pdf")})
        assert response.status_code==403
