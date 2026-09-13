from __future__ import annotations
from collections.abc import Iterator
import re,zipfile,xml.etree.ElementTree as ET
from .models import ElementRef,PageRecord,stable_id
_W="{http://schemas.openxmlformats.org/wordprocessingml/2006/main}";_R="{http://schemas.openxmlformats.org/officeDocument/2006/relationships}";_A="{http://schemas.openxmlformats.org/drawingml/2006/main}";_C="{http://schemas.openxmlformats.org/drawingml/2006/chart}";_REL_NS="{http://schemas.openxmlformats.org/package/2006/relationships}"
def _text_from_element(elem):
    texts=[]
    for node in elem.iter():
        if node.tag==_W+"t" and node.text:texts.append(node.text)
        elif node.tag==_W+"tab":texts.append("\t")
        elif node.tag in {_W+"br",_W+"cr"}:texts.append("\n")
    return re.sub(r"[ \t]+"," ","".join(texts)).strip()
def _table_markdown(tbl):
    rows=[]
    for tr in tbl.findall(".//"+_W+"tr"):
        row=[_text_from_element(tc).replace("\n"," ").strip() for tc in tr.findall("./"+_W+"tc")]
        if row:rows.append(row)
    if not rows:return "",[]
    width=max(map(len,rows));padded=[r+[""]*(width-len(r)) for r in rows];lines=["| "+" | ".join(c.replace("|","\\|") for c in r)+" |" for r in padded]
    if len(lines)>=2:lines.insert(1,"|"+"|".join(" --- " for _ in range(width))+"|")
    return "\n".join(lines),rows
def _relationships(zf):
    try:root=ET.fromstring(zf.read("word/_rels/document.xml.rels"))
    except KeyError:return {}
    rels={}
    for rel in root.findall(_REL_NS+"Relationship"):
        rid=rel.attrib.get("Id");target=rel.attrib.get("Target","")
        if rid and target and not target.startswith("http"):
            target=target.lstrip("/");target=target if target.startswith("word/") else "word/"+target;parts=[]
            for part in target.split("/"):
                if part==".." and parts:parts.pop()
                elif part not in {".",""}:parts.append(part)
            rels[rid]="/".join(parts)
    return rels
def _linked_assets(elem,rels):
    out=[]
    for node in elem.iter():
        if node.tag==_A+"blip":
            rid=node.attrib.get(_R+"embed")
            if rid and rid in rels:out.append(("image",rels[rid]))
        elif node.tag==_C+"chart":
            rid=node.attrib.get(_R+"id")
            if rid and rid in rels:out.append(("chart",rels[rid]))
    seen=set();unique=[]
    for x in out:
        if x not in seen:unique.append(x);seen.add(x)
    return unique
class DOCXPageStream:
    def __init__(self,file_path,doc_id):self.file_path=file_path;self.doc_id=doc_id
    def __iter__(self)->Iterator[PageRecord]:
        page_no=1;order=0;current=[];current_text=[]
        with zipfile.ZipFile(self.file_path) as zf:
            rels=_relationships(zf)
            with zf.open("word/document.xml") as xml:
                context=ET.iterparse(xml,events=("start","end"));table_depth=0;stack=[]
                for event,elem in context:
                    if event=="start":stack.append(elem);table_depth+=1 if elem.tag==_W+"tbl" else 0;continue
                    if elem.tag==_W+"p" and table_depth==0:
                        text=_text_from_element(elem)
                        if text:current.append(ElementRef(stable_id("el",self.doc_id,page_no,order,text[:100]),"text",None,order,text,extraction_status="native",raw={"docx_logical_page":True}));current_text.append(text);order+=1
                        for typ,zp in _linked_assets(elem,rels):current.append(ElementRef(stable_id("el",self.doc_id,page_no,order,typ,zp),typ,None,order,source_locator={"kind":"docx_media" if typ=="image" else "docx_chart_xml","zip_path":zp},extraction_status="pending",raw={"docx_logical_page":True}));order+=1
                        br=any(n.tag==_W+"br" and n.attrib.get(_W+"type")=="page" for n in elem.iter())
                        if len(stack)>=2:
                            try:stack[-2].remove(elem)
                            except ValueError:pass
                        elem.clear()
                        if br:yield PageRecord(page_no,str(page_no),0,0,current,"\n".join(current_text));page_no+=1;order=0;current=[];current_text=[]
                    elif elem.tag==_W+"tbl":
                        if table_depth==1:
                            text,rows=_table_markdown(elem)
                            if text:current.append(ElementRef(stable_id("el",self.doc_id,page_no,order,text[:100]),"table",None,order,text,extraction_status="success",raw={"docx_logical_page":True,"rows":rows,"row_count":len(rows),"col_count":max((len(r) for r in rows),default=0)}));current_text.append(text);order+=1
                            for typ,zp in _linked_assets(elem,rels):current.append(ElementRef(stable_id("el",self.doc_id,page_no,order,typ,zp),typ,None,order,source_locator={"kind":"docx_media" if typ=="image" else "docx_chart_xml","zip_path":zp},extraction_status="pending",raw={"docx_logical_page":True}));order+=1
                            br=any(n.tag==_W+"br" and n.attrib.get(_W+"type")=="page" for n in elem.iter())
                            if len(stack)>=2:
                                try:stack[-2].remove(elem)
                                except ValueError:pass
                            elem.clear()
                            if br:yield PageRecord(page_no,str(page_no),0,0,current,"\n".join(current_text));page_no+=1;order=0;current=[];current_text=[]
                        table_depth-=1
                    if stack and stack[-1] is elem:stack.pop()
        if current or page_no==1:yield PageRecord(page_no,str(page_no),0,0,current,"\n".join(current_text))
class DOCXEnricher:
    def __init__(self,file_path,vision_ocr=None):self.file_path=file_path;self.vision_ocr=vision_ocr
    def enrich_page(self,page):
        with zipfile.ZipFile(self.file_path) as zf:
            for el in page.elements:
                if el.extraction_status!="pending":continue
                try:
                    loc=el.source_locator
                    if loc.get("kind")=="docx_media" and self.vision_ocr:
                        result=self.vision_ocr(zf.read(loc["zip_path"]));el.text=str(getattr(result,"text",result or ""));el.confidence=getattr(result,"confidence",None);success=bool(getattr(result,"success",True));low=str(getattr(result,"error",""))=="low_confidence";el.extraction_status="low_confidence" if success and low else ("success" if success else "error")
                    elif loc.get("kind")=="docx_chart_xml":
                        root=ET.fromstring(zf.read(loc["zip_path"]));labels=[n.text.strip() for n in root.iter() if n.tag==_A+"t" and n.text and n.text.strip()];values=[n.text.strip() for n in root.iter() if n.tag==_C+"v" and n.text and n.text.strip()];parts=[]
                        if labels:parts.append("Labels: "+" | ".join(labels))
                        if values:parts.append("Values: "+" | ".join(values))
                        el.text="[DOCX CHART DATA]\n"+"\n".join(parts) if parts else "";el.extraction_status="success" if el.text else "missing"
                except Exception as exc:el.extraction_status="error";el.raw["error"]=str(exc)
        if any(e.element_type in {"image","chart"} and e.text for e in page.elements):page.native_text="\n".join([page.native_text]+[e.text for e in page.elements if e.element_type in {"image","chart"} and e.text]).strip()
        page.extraction_status="enriched";return page
