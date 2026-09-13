from __future__ import annotations
from collections.abc import Iterable,Iterator
import re
from .models import ChunkRecord,ElementRef,PageRecord,SemanticUnit,stable_id
from .tokenizer import TokenCounter
_HEADING=re.compile(r"^(?:\d+(?:\.\d+){0,4}\s+\S|SUMMARY$|INTRODUCTION$|PROTOCOL$|STUDY ADMINISTRATION|REPORT APPROVAL|RESULTS?$|DISCUSSION$|CONCLUSIONS?$|APPENDIX|ANNEX|CHAPTER|SECTION)",re.I)
def _norm_sig(v):return re.sub(r"\d+","#",re.sub(r"\s+"," ",(v or "").strip().casefold()))
def repeated_signatures(pages,min_fraction=.5,min_pages=3):
    pages=list(pages)
    if len(pages)<min_pages:return set(),set()
    h={};f={}
    for p in pages:
        if p.header_candidate:s=_norm_sig(p.header_candidate);h[s]=h.get(s,0)+1
        if p.footer_candidate:s=_norm_sig(p.footer_candidate);f[s]=f.get(s,0)+1
    t=max(min_pages,int(len(pages)*min_fraction+.999));return {k for k,v in h.items() if v>=t},{k for k,v in f.items() if v>=t}
def _bbox_overlap_fraction(a,b):
    if not a or not b:return 0
    x0=max(a.x0,b.x0);y0=max(a.y0,b.y0);x1=min(a.x1,b.x1);y1=min(a.y1,b.y1);inter=max(0,x1-x0)*max(0,y1-y0);area=max(0,a.x1-a.x0)*max(0,a.y1-a.y0);return inter/area if area else 0
def _table_continues(prev,pp,cur,cp):
    if prev.element_type!="table" or cur.element_type!="table" or not prev.bbox or not cur.bbox or cp.page_number!=pp.page_number+1 or pp.height<=0 or cp.height<=0:return False
    if not(prev.bbox.y1>=pp.height*.88 and cur.bbox.y0<=cp.height*.15):return False
    pc=int(prev.raw.get("col_count",0));cc=int(cur.raw.get("col_count",0))
    if not pc or pc!=cc:return False
    pr=prev.raw.get("rows") or [];cr=cur.raw.get("rows") or []
    if not pr or not cr:return False
    ph=[str(x).strip().casefold() for x in pr[0]];ch=[str(x).strip().casefold() for x in cr[0]];return ph==ch or sum(bool(x) for x in ch)<max(1,len(ch)//2)
class PDFMarkdown:
    @staticmethod
    def table(rows):
        if not rows:return ""
        w=max(map(len,rows));p=[list(r)+[""]*(w-len(r)) for r in rows];lines=["| "+" | ".join(str(c).replace("|","\\|") for c in r)+" |" for r in p]
        if len(lines)>=2:lines.insert(1,"|"+"|".join(" --- " for _ in range(w))+"|")
        return "\n".join(lines)
class SemanticUnitStream:
    def __init__(self,doc_id,source_url="",header_sigs=None,footer_sigs=None,table_summarizer=None):self.doc_id=doc_id;self.source_url=source_url;self.header_sigs=header_sigs or set();self.footer_sigs=footer_sigs or set();self.table_summarizer=table_summarizer
    def from_pages(self,pages)->Iterator[SemanticUnit]:
        section=[];pending=None
        def flush():
            nonlocal pending
            if pending is None:return None
            _,_,pgs,els=pending;rows=[];first=None;bboxes=[]
            for p,e in zip(pgs,els):
                rr=e.raw.get("rows") or []
                if rr:
                    hdr=rr[0]
                    if first is None:first=hdr
                    elif [str(x).strip().casefold() for x in hdr]==[str(x).strip().casefold() for x in first]:rr=rr[1:]
                    rows.extend(rr)
                if e.bbox:bboxes.append({"page":p.page_number,"bbox":e.bbox.to_list()})
            text=PDFMarkdown.table(rows);summary=self.table_summarizer(text,pgs[0].page_number,pgs[-1].page_number) if self.table_summarizer else ""
            if summary:text+=f"\n\n[TABLE SUMMARY]\n{summary}"
            u=SemanticUnit(stable_id("unit",self.doc_id,"table",pgs[0].page_number,pgs[-1].page_number,els[0].element_id),"table",text,pgs[0].page_number,pgs[-1].page_number,[p.page_label for p in pgs],list(section),bboxes,True,self.source_url,{"multi_page":len(pgs)>1});pending=None;return u
        for page in pages:
            hd=_norm_sig(page.header_candidate) in self.header_sigs if page.header_candidate else False;fd=_norm_sig(page.footer_candidate) in self.footer_sigs if page.footer_candidate else False;tables=[e for e in page.elements if e.element_type=="table" and e.text and e.extraction_status=="success"];others=[e for e in page.elements if e.element_type!="table" and e.text and not any(_bbox_overlap_fraction(e.bbox,t.bbox)>=.5 for t in tables)]
            for t in tables:
                if pending:
                    _,_,pgs,els=pending
                    if _table_continues(els[-1],pgs[-1],t,page):pgs.append(page);els.append(t);continue
                    u=flush()
                    if u:yield u
                pending=(page,t,[page],[t])
            for e in others:
                text=e.text.strip()
                if not text or (hd and text in page.header_candidate) or (fd and text in page.footer_candidate):continue
                if _HEADING.match(text) and len(text)<=200:section=[text];yield SemanticUnit(stable_id("unit",self.doc_id,page.page_number,e.element_id),"heading",text,page.page_number,page.page_number,[page.page_label],list(section),source_url=self.source_url);continue
                typ="image" if e.element_type=="image" else("chart" if e.element_type=="chart" else "paragraph");yield SemanticUnit(stable_id("unit",self.doc_id,page.page_number,e.element_id),typ,text,page.page_number,page.page_number,[page.page_label],list(section),[{"page":page.page_number,"bbox":e.bbox.to_list()}] if e.bbox else [],True,self.source_url)
        u=flush()
        if u:yield u
class Chunker:
    def __init__(self,doc_id,generation_id,min_tokens=1200,max_tokens=1500,overlap_tokens=100,token_counter=None):self.doc_id=doc_id;self.generation_id=generation_id;self.min_tokens=min_tokens;self.max_tokens=max_tokens;self.overlap_tokens=overlap_tokens;self.tokens=token_counter or TokenCounter()
    def chunks(self,units)->Iterator[ChunkRecord]:
        cur=[];cur_n=0;idx=0
        def emit(buf,i):
            text="\n\n".join(u.text for u in buf if u.text);pages=[p for u in buf for p in range(u.page_start,u.page_end+1)];labels=[];seen=set()
            for u in buf:
                for l in u.page_labels:
                    if l not in seen:labels.append(l);seen.add(l)
            types=sorted({u.unit_type for u in buf});tb=[b for u in buf if u.unit_type=="table" for b in u.bboxes];section=next((u.section_path for u in reversed(buf) if u.section_path),[]);over=self.tokens.count(text)>self.max_tokens
            return ChunkRecord(stable_id("chunk",self.doc_id,self.generation_id,i,buf[0].unit_id,buf[-1].unit_id),self.doc_id,self.generation_id,i,text,min(pages),max(pages),labels,section,types,buf[0].source_url if buf else "",tb,{"oversize_atomic":over and len(buf)==1})
        for u in units:
            n=self.tokens.count(u.text)
            if n>self.max_tokens:
                if cur:yield emit(cur,idx);idx+=1;cur=[];cur_n=0
                yield emit([u],idx);idx+=1;continue
            if cur and cur_n+n>self.max_tokens:
                prev=cur;yield emit(prev,idx);idx+=1;overlap=[];on=0
                for old in reversed(prev):
                    t=self.tokens.count(old.text)
                    if overlap and on+t>self.overlap_tokens:break
                    overlap.insert(0,old);on+=t
                    if on>=self.overlap_tokens:break
                cur=overlap;cur_n=on
            cur.append(u);cur_n+=n
        if cur:yield emit(cur,idx)
