from __future__ import annotations
import json
import re
from typing import Any, Iterable
from .azure_gateway import AzureGateway
from .metadata_schema import FIELDS, BY_KEY, BY_LABEL_CASEFOLD
from .models import MetadataValue, PageRecord


def _present(value: Any) -> bool:
    if value is None: return False
    if isinstance(value,str): return bool(value.strip())
    if isinstance(value,(list,tuple,dict,set)): return bool(value)
    return True


def overlap_with_rimdocs(rimdocs: dict[str, Any]) -> tuple[dict[str, MetadataValue], list[str]]:
    normalized={}
    for key,value in rimdocs.items():
        if key in BY_KEY: normalized[key]=value; continue
        spec=BY_LABEL_CASEFOLD.get(str(key).casefold())
        if spec: normalized[spec.key]=value
    resolved={}; missing=[]
    for spec in FIELDS:
        value=normalized.get(spec.key)
        if _present(value): resolved[spec.key]=MetadataValue(spec.key,spec.source_label,value,value,source="rimdocs",status="authoritative",normalization_status="not_applicable")
        else: missing.append(spec.key)
    return resolved,missing

_SECTION_TERMS=("summary","study administration","study administration section","protocol","report approval","report approvals")


def candidate_evidence_pages(pages: Iterable[PageRecord], max_fallback_pages: int=20) -> list[PageRecord]:
    fallback=[]; selected={}; prev=None; include_next=False
    for page in pages:
        if len(fallback)<max_fallback_pages: fallback.append(page)
        if include_next: selected[page.page_number]=page; include_next=False
        text=page.native_text.casefold()
        if any(term in text for term in _SECTION_TERMS):
            if prev is not None: selected[prev.page_number]=prev
            selected[page.page_number]=page; include_next=True
        prev=page
    return [selected[n] for n in sorted(selected)] if selected else fallback


class MetadataExtractor:
    def __init__(self,gateway:AzureGateway): self.gateway=gateway
    def extract_missing_stream(self,missing_keys:list[str],evidence_pages:Iterable[PageRecord],batch_pages:int=6)->dict[str,MetadataValue]:
        unresolved=list(missing_keys); resolved={}; batch=[]
        for page in evidence_pages:
            batch.append(page)
            if len(batch)<batch_pages: continue
            partial=self.extract_missing(unresolved,batch)
            for key,value in partial.items():
                if value.status=="extracted": resolved[key]=value
            unresolved=[k for k in unresolved if k not in resolved]
            if not unresolved: return resolved
            batch=[]
        if batch and unresolved:
            partial=self.extract_missing(unresolved,batch)
            for key,value in partial.items():
                if value.status=="extracted": resolved[key]=value
        for key in missing_keys:
            if key not in resolved:
                spec=BY_KEY[key]; resolved[key]=MetadataValue(spec.key,spec.source_label,None,source="missing",status="missing")
        return resolved

    def extract_missing(self,missing_keys:list[str],evidence_pages:list[PageRecord])->dict[str,MetadataValue]:
        if not missing_keys:return {}
        specs=[BY_KEY[k] for k in missing_keys]; evidence_parts=[]
        for p in evidence_pages:
            text=p.native_text.strip()
            if text:evidence_parts.append(f"[PAGE {p.page_number}]\n{text}")
        evidence="\n\n".join(evidence_parts)
        if not evidence:return {s.key:MetadataValue(s.key,s.source_label,None,source="missing",status="missing") for s in specs}
        schema=[{"key":s.key,"source_label":s.source_label,"data_type":s.data_type} for s in specs]
        system="You extract regulatory-study metadata from supplied document evidence only. Never guess. For any field without direct support, return null. Do not turn examples or expectations into facts. Return JSON only."
        user={"requested_fields":schema,"instructions":{"output":"object keyed by canonical key","per_field":["value","raw_value","unit","evidence_text","evidence_pages","confidence"],"evidence_rule":"evidence_text must be a short exact-supporting excerpt from the supplied pages; null value when unsupported","numeric_rule":"retain raw_value and separate numeric value/unit when evidence includes units"},"document_evidence":evidence}
        result=self.gateway.chat_json([{"role":"system","content":system},{"role":"user","content":json.dumps(user,ensure_ascii=False)}],max_tokens=5000,temperature=0.0)
        out={}
        for spec in specs:
            item=result.get(spec.key) if isinstance(result,dict) else None
            if not isinstance(item,dict) or not _present(item.get("value")):
                out[spec.key]=MetadataValue(spec.key,spec.source_label,None,source="missing",status="missing");continue
            pages=[int(x) for x in item.get("evidence_pages",[]) if str(x).isdigit()]; valid_pages={p.page_number for p in evidence_pages}; evidence_text=str(item.get("evidence_text","")).strip()
            def norm(v:str)->str:return re.sub(r"\s+"," ",v).strip().casefold()
            page_text_by_num={p.page_number:norm(p.native_text) for p in evidence_pages}; evidence_supported=bool(evidence_text) and any(norm(evidence_text) in page_text_by_num.get(n,"") for n in pages)
            if not pages or not set(pages).issubset(valid_pages) or not evidence_supported:
                out[spec.key]=MetadataValue(spec.key,spec.source_label,None,source="missing",status="invalid");continue
            out[spec.key]=MetadataValue(spec.key,spec.source_label,item.get("value"),item.get("raw_value",item.get("value")),item.get("unit"),"llm","extracted",evidence_text,pages,float(item["confidence"]) if isinstance(item.get("confidence"),(int,float)) else None,"normalized" if item.get("unit") is not None else "not_applicable")
        return out
