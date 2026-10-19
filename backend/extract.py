"""EvidenceFlow: conservative, proof-carrying PDF extraction."""
from __future__ import annotations
import io
import re
from datetime import datetime
from typing import Any
import fitz
from dateutil.parser import parse as date_parse
from . import config
from .integrations import PermanentError

FIELD_NAMES=("document_date","international_birth_date","product","active_ingredient","manufacturer","document_number")
CRITICAL=("document_date","international_birth_date","product")
MONTHS=r"(?:Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|Jun(?:e)?|Jul(?:y)?|Aug(?:ust)?|Sep(?:tember)?|Oct(?:ober)?|Nov(?:ember)?|Dec(?:ember)?)"
DATE=rf"(?:\d{{1,2}}\s+{MONTHS}\s+\d{{4}}|{MONTHS}\s+\d{{1,2}},?\s+\d{{4}}|\d{{4}}-\d{{2}}-\d{{2}})"
RULES={
    "international_birth_date": re.compile(rf"\bInternational\s+Birth\s+Date\b\s*(?:(?:is|:|\-|=)\s*)({DATE})",re.I),
    "document_date":re.compile(rf"\bStatus\s*:\s*Approved\s*,?\s*Date\s*:\s*({DATE})",re.I),
    "active_ingredient":re.compile(r"\bActive\s+ingredient\s*[:\-]\s*([A-Za-z][A-Za-z -]{2,80})",re.I),
    "manufacturer":re.compile(r"\bManufacturer\s*[:\-]\s*([^\n]{3,120})",re.I),
    "document_number":re.compile(r"\b(?:Document\s+(?:No\.|Number)|EDMS)\s*[:#-]?\s*([A-Za-z0-9-]{5,55})",re.I)
}
PRODUCT_LABEL=re.compile(r"\b(?:Product|Trade\s+name)\s*:\s*([A-Za-z][A-Za-z0-9® -]{2,70})",re.I)
PRODUCT_TITLE=re.compile(r"^\s*([A-Z][A-Z0-9-]{2,35})\s*[®™]?\s*\(([A-Za-z][A-Za-z -]{2,70})\)",re.M)

def norm_date(s):
    try:return date_parse(s,dayfirst=bool(re.match(r"^\d{1,2}\s",s)),fuzzy=False).date().isoformat()
    except (ValueError,OverflowError,TypeError):return None

def evidence(value,page,quote,method="RULE",validation="VERIFIED"):
    return dict(value=value,display=value,page=page,excerpt=quote.strip(),method=method,validation=validation)

def native_pages(content:bytes)->tuple[list[dict],list[int]]:
    try:pdf=fitz.open(stream=content,filetype="pdf")
    except Exception as e:raise PermanentError("Unreadable or corrupt PDF") from e
    with pdf:
        if pdf.needs_pass:raise PermanentError("Encrypted PDF needs password")
        if pdf.page_count>config.MAX_PAGES:raise PermanentError("Too many PDF pages")
        pages=[];poor=[]
        for i,page in enumerate(pdf):
            raw=page.get_text("text",sort=True) or ""
            pages.append({"page":i+1,"text":raw})
            if len(re.sub(r"\s+","",raw))<40 and page.get_images():poor.append(i+1)
        return pages,poor

def ocr_missing_pages(content:bytes,pages:list[dict],poor:list[int]):
    """Google Vision OCR is optional, invoked only for poor native-text pages."""
    if not poor:return pages,False
    if not config.OCR_ENABLED:return pages,True
    if len(poor)>config.MAX_OCR_PAGES: return pages,True
    try:
        from google.cloud import vision
        client=vision.ImageAnnotatorClient()
        with fitz.open(stream=content,filetype="pdf") as pdf:
            for n in poor:
                png=pdf[n-1].get_pixmap(matrix=fitz.Matrix(1.7,1.7)).tobytes("png")
                response=client.document_text_detection(image=vision.Image(content=png))
                if response.error.message:raise RuntimeError(response.error.message)
                pages[n-1]["text"]=response.full_text_annotation.text or ""
    except Exception as e:
        raise RuntimeError("Configured OCR failed") from e
    return pages,any(len(re.sub(r"\s+","",p["text"]))<40 for p in pages)

def _excerpt(text,start,end):
    # Small proof span, avoiding full document text in result stores.
    left=text.rfind('\n',max(0,start-120),start)
    right=text.find('\n',end,min(len(text),end+180))
    return text[max(0,left+1 if left!=-1 else start-80): right if right!=-1 else min(len(text),end+140)].strip()[:360]

def rule_extract(pages:list[dict])->dict[str,dict]:
    found={}
    for p in pages:
        text=p["text"]
        for name,pat in RULES.items():
            if name in found:continue
            m=pat.search(text)
            if not m:continue
            raw=m.group(1).strip().split('\n')[0].strip()
            value=norm_date(raw) if name.endswith("date") else raw
            if value: found[name]=evidence(value,p["page"],_excerpt(text,*m.span()))
        if "product" not in found:
            match=PRODUCT_LABEL.search(text) or PRODUCT_TITLE.search(text)
            if match:
                product=match.group(1).strip().rstrip('®™').strip()
                if product:found["product"]=evidence(product,p["page"],_excerpt(text,*match.span()))
                if match.re==PRODUCT_TITLE and "active_ingredient" not in found:
                    found["active_ingredient"]=evidence(match.group(2).strip(),p["page"],_excerpt(text,*match.span()))
    return found

def _squash(s:str):return re.sub(r"\s+"," ",str(s)).strip().casefold()

def _verify_llm_field(name:str,item:dict,pages:list[dict]):
    if not isinstance(item,dict):return None
    val=item.get("value");quote=item.get("quote");page=item.get("page")
    if not val or not isinstance(quote,str) or not isinstance(page,int):return None
    match=next((p for p in pages if p["page"]==page),None)
    if not match or _squash(quote) not in _squash(match["text"]):return None
    raw=str(val).strip()
    if name.endswith("date"):
        # Date may be normalized, but an actual labeled date must be present in the quote.
        required="international birth date" if name=="international_birth_date" else "date"
        if required not in _squash(quote):return None
        dates=re.findall(DATE,quote,re.I)
        if not dates:return None
        value=norm_date(raw)
        if not value or value not in [norm_date(d) for d in dates]:return None
    else:
        if _squash(raw) not in _squash(quote):return None
        value=raw
    return evidence(value,page,quote,"AZURE_LLM_EVIDENCE")

def extract_document(content:bytes,llm=None):
    pages,poor=native_pages(content)
    pages,ocr_incomplete=ocr_missing_pages(content,pages,poor)
    fields=rule_extract(pages)
    summary=None;note=None
    if llm:
        try:
            response,usage=llm.extract(pages,fields)
            for name in FIELD_NAMES:
                if name in fields:continue
                checked=_verify_llm_field(name,(response.get("fields") or {}).get(name),pages)
                if checked:fields[name]=checked
            suggestion=response.get("summary") or {}
            if isinstance(suggestion,dict):
                candidate=suggestion.get("text","")
                cited=suggestion.get("evidence_pages",[])
                if isinstance(candidate,str) and candidate.strip() and isinstance(cited,list) and cited and all(type(x)==int and 1<=x<=len(pages) for x in cited):
                    summary=candidate.strip()[:1000]
                    note=f"AI summary is a grounded draft; cited pages: {', '.join(map(str,cited))}. Human review may be required."
        except (ValueError,KeyError,TypeError,RuntimeError) as e:
            note="AI extraction could not be completed; review required."
            # No silent fabricated fallback text.
    missing=[x for x in CRITICAL if x not in fields]
    if missing or ocr_incomplete or not summary:
        status="REVIEW_REQUIRED"
        details=[]
        if missing:details.append("Missing verified fields: "+", ".join(missing))
        if ocr_incomplete:details.append("Some pages lack readable text; OCR needed")
        if not summary:details.append("Summary unavailable or not grounded")
        note="; ".join(details)+(f"; {note}" if note else "")
    else:status="COMPLETED"
    return fields,summary,status,note
