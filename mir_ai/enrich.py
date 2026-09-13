from __future__ import annotations
import threading
from .models import BBox,ElementRef,PageRecord,stable_id
_tls=threading.local()
class PDFEnricher:
    def __init__(self,file_path,doc_id,ocr_render_dpi=200,vision_ocr=None,chart_describer=None):self.file_path=file_path;self.doc_id=doc_id;self.ocr_render_dpi=ocr_render_dpi;self.vision_ocr=vision_ocr;self.chart_describer=chart_describer
    def _handles(self):
        key=f"handles_{id(self)}";h=getattr(_tls,key,None)
        if h is None:
            import fitz,pdfplumber
            h=(fitz.open(self.file_path),pdfplumber.open(self.file_path));setattr(_tls,key,h)
        return h
    def enrich_page(self,page:PageRecord)->PageRecord:
        if page.error:return page
        fitz_doc,plumb_doc=self._handles();idx=page.page_number-1;fitz_page=fitz_doc[idx]
        try:
            pp=plumb_doc.pages[idx];found=pp.find_tables({"vertical_strategy":"lines","horizontal_strategy":"lines","snap_tolerance":3})
            if not found:found=pp.find_tables({"vertical_strategy":"text","horizontal_strategy":"text","intersection_tolerance":5})
            for ti,t in enumerate(found):
                bb=t.bbox;rows=t.extract() or [];clean=[["" if c is None else str(c).replace("\u00ad","").strip() for c in (r or [])] for r in rows]
                if not any(any(c for c in r) for r in clean):continue
                bbox=BBox(float(bb[0]),float(bb[1]),float(bb[2]),float(bb[3]));page.elements.append(ElementRef(stable_id("el",self.doc_id,page.page_number,"table",ti,bbox.to_list()),"table",bbox,len(page.elements),self._to_markdown(clean),extraction_status="success",raw={"rows":clean,"row_count":len(clean),"col_count":max((len(r) for r in clean),default=0),"page_height":page.height}))
        except Exception as exc:page.elements.append(ElementRef(stable_id("el",self.doc_id,page.page_number,"table_error"),"table",None,len(page.elements),"",extraction_status="error",raw={"error":str(exc)}))
        targets=[e for e in page.elements if e.element_type in {"image","chart"} and e.extraction_status=="pending"]
        if page.needs_full_ocr:targets=[e for e in targets if e.raw.get("full_page_ocr")]
        if self.vision_ocr:
            for el in targets:
                try:
                    import fitz
                    loc=el.source_locator;b=el.bbox;mat=fitz.Matrix(self.ocr_render_dpi/72.0,self.ocr_render_dpi/72.0);pix=fitz_page.get_pixmap(matrix=mat,alpha=False) if loc.get("kind")=="pdf_full_page" else fitz_page.get_pixmap(matrix=mat,clip=fitz.Rect(*b.to_list()),alpha=False);data=pix.tobytes("png");result=self.vision_ocr(data);ocr=str(getattr(result,"text",result or ""));el.confidence=getattr(result,"confidence",None);success=bool(getattr(result,"success",True))
                    if el.element_type=="chart" and self.chart_describer:nearby=" ".join(e.text for e in page.elements if e.element_type=="text" and e.text)[:1000];desc=self.chart_describer(data,nearby);el.text=(f"[CHART DESCRIPTION]\n{desc}\n\n[VISIBLE OCR TEXT]\n{ocr}" if desc else ocr).strip()
                    else:el.text=ocr
                    low=str(getattr(result,"error",""))=="low_confidence";el.extraction_status="low_confidence" if success and low else ("success" if success and el.text else ("low_confidence" if success else "error"));del data;pix=None
                except Exception as exc:el.extraction_status="error";el.raw["error"]=str(exc)
        page.elements.sort(key=lambda e:((e.bbox.y0 if e.bbox else 0),(e.bbox.x0 if e.bbox else 0),e.reading_order))
        for i,e in enumerate(page.elements):e.reading_order=i
        if page.needs_full_ocr:
            full=next((e for e in page.elements if e.raw.get("full_page_ocr") and e.text),None)
            if full:page.native_text=full.text
        page.extraction_status="enriched";return page
    @staticmethod
    def _to_markdown(rows):
        width=max((len(r) for r in rows),default=0)
        if not width:return ""
        padded=[r+[""]*(width-len(r)) for r in rows];lines=["| "+" | ".join(c.replace("|","\\|") for c in r)+" |" for r in padded]
        if len(lines)>=2:lines.insert(1,"|"+"|".join(" --- " for _ in range(width))+"|")
        return "\n".join(lines)
