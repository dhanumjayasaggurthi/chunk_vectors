"""Bounded, derivative-only DOCX evidence; no pagination or original ZIP copy.

Preserve source structural tokens, physical cell hierarchy, relationship links,
image bytes, and chart-cache values. This is not a Word renderer or proof of
complete semantic extraction. Unsupported content remains an explicit blocker.
"""
from __future__ import annotations

import argparse
import json
import posixpath
import re
import time
import zipfile
from pathlib import Path
from xml.etree import ElementTree as ET

from visual_evidence import _sha, _json_bytes, _hash_id, _write_once, digest_file, _source_state

W="http://schemas.openxmlformats.org/wordprocessingml/2006/main"
R="http://schemas.openxmlformats.org/officeDocument/2006/relationships"
A="http://schemas.openxmlformats.org/drawingml/2006/main"
C="http://schemas.openxmlformats.org/drawingml/2006/chart"
P="http://schemas.openxmlformats.org/package/2006/relationships"
NS={W:"w",R:"r",A:"a",C:"c"}
SCHEMA="docx-extracted-evidence-v1"
LIMITS={"max_package_bytes":(1024,2_000_000_000),
        "max_uncompressed_bytes":(1024,2_000_000_000),
        "max_parts":(1,20000),"max_xml_bytes":(1024,100_000_000),
        "max_asset_bytes":(1024,100_000_000),"max_manifest_bytes":(1024,200_000_000),
        "max_xml_nodes":(1,2_000_000),"max_depth":(1,128)}
# Semantic structural whitelist. No opaque XML, styles package, or embedded
# Office files are retained. Unknown content is reported, not silently certified.
W_TAGS=set("p r t tab br cr noBreakHyphen softHyphen pPr rPr pStyle rStyle b i u strike dstrike color sz szCs rFonts lang vertAlign highlight vanish rtl bidi jc numPr numId ilvl outlineLvl spacing ind keepNext keepLines pageBreakBefore tbl tblPr tblGrid gridCol tblStyle tblW tblLayout tblBorders tr trPr tblHeader gridBefore gridAfter tc tcPr tcW gridSpan vMerge hMerge vAlign shd hyperlink fldSimple fldChar instrText bookmarkStart bookmarkEnd footnoteReference endnoteReference commentReference drawing pict object sdt sdtContent ins del delText moveFrom moveTo txbxContent sectPr headerReference footerReference footnote endnote comment".split())
TEXT_TAGS={f"{{{W}}}{x}" for x in ("t","instrText","delText")}|{f"{{{A}}}t"}
STORY=re.compile(r"word/(?:document|header[0-9]+|footer[0-9]+|footnotes|endnotes|comments)\.xml$")
CHART=re.compile(r"word/charts/chart[0-9]+\.xml$")
IMAGE_EXT={".png",".jpg",".jpeg",".gif",".bmp",".tif",".tiff",".emf",".wmf",".svg"}


def settings(value):
    if not isinstance(value,dict) or set(value)!=set(LIMITS):
        raise ValueError("Expected complete DOCX evidence profile")
    for k,(lo,hi) in LIMITS.items():
        if type(value[k]) is not int or not lo<=value[k]<=hi:
            raise ValueError(f"Invalid DOCX evidence {k}")
    return dict(value)


def _load_profile(path):
    with Path(path).open("rb") as stream:
        raw=stream.read(65537)
    if len(raw)>65536:
        raise ValueError("DOCX profile exceeds limit")
    return settings(json.loads(raw))


def _qualified(tag):
    if tag.startswith("{"):
        ns,name=tag[1:].split("}",1)
        return NS.get(ns,ns)+":"+name
    return tag


def _read(z,name,limit):
    info=z.getinfo(name)
    if info.file_size>limit:
        raise ValueError("DOCX part exceeds configured byte limit")
    with z.open(info) as stream:
        data=stream.read(limit+1)
    if len(data)!=info.file_size or len(data)>limit:
        raise ValueError("DOCX part size mismatch")
    return data


def _xml(z,name,cfg):
    # Accept XML encoded as UTF-8 (including BOM); reject DTD/entity declarations
    # before parsing. No network resolution, no untrusted ZIP extraction.
    text=_read(z,name,cfg["max_xml_bytes"]).decode("utf-8-sig")
    if "\x00" in text or re.search(r"<!\s*(?:DOCTYPE|ENTITY)\b",text,re.I):
        raise ValueError("Unsupported XML encoding or forbidden DTD/entity")
    root=ET.fromstring(text)
    count=0
    stack=[(root,1)]
    while stack:
        node,depth=stack.pop()
        count+=1
        if count>cfg["max_xml_nodes"] or depth>cfg["max_depth"]:
            raise ValueError("DOCX XML structure limit exceeded")
        stack.extend((child,depth+1) for child in node)
    return root


def _rels(z,part,names,cfg):
    folder,filename=posixpath.split(part)
    rel=posixpath.join(folder,"_rels",filename+".rels")
    if rel not in names:
        return {}
    root=_xml(z,rel,cfg)
    result={}
    for node in root.findall(f"{{{P}}}Relationship"):
        rid=node.get("Id")
        target=node.get("Target","")
        if not rid or rid in result:
            raise ValueError("Invalid duplicate DOCX relationship")
        external=node.get("TargetMode","Internal")=="External"
        if external:
            # Never download or disclose external target URI.
            resolved=None
        else:
            if "\\" in target or ":" in target or not target:
                raise ValueError("Unsupported internal DOCX relationship")
            resolved=posixpath.normpath(target.lstrip("/") if target.startswith("/")
                                         else posixpath.join(folder,target))
            if resolved.startswith("../") or resolved not in names:
                resolved=None
        result[rid]={"kind":node.get("Type","").rsplit("/",1)[-1],
                     "target_part":resolved,"external":external}
    return result


def _project(node,counts,unsupported,*,chart=False):
    """Ordered extracted JSON tokens, retaining hierarchy without XML bytes."""
    tag=node.tag
    name=_qualified(tag)
    if chart:
        keep=tag.startswith("{"+C+"}") or tag in TEXT_TAGS
    else:
        keep=(tag.startswith("{"+W+"}") and tag.split("}",1)[1] in W_TAGS
              or tag.startswith("{"+A+"}") and tag.split("}",1)[1] in {"blip","t"}
              or tag==f"{{{C}}}chart"
              or any(k.startswith("{"+R+"}") for k in node.attrib))
    children=[]
    for child in node:
        children.extend(_project(child,counts,unsupported,chart=chart))
    if not keep:
        # Unknown semantic containers are visible in the inventory. Pure
        # wrappers are flattened; retained children keep document order.
        unsupported[name]=unsupported.get(name,0)+1
        return children
    counts[name]=counts.get(name,0)+1
    out={"tag":name}
    if node.attrib:
        out["attributes"]={_qualified(k):v for k,v in node.attrib.items()}
    if node.text is not None and (tag in TEXT_TAGS or chart and not children and node.text.strip()):
        out["text"]=node.text
    if children:
        out["children"]=children
    return [out]


def _folder(root,cid):
    _hash_id(cid)
    return Path(root)/"docx"/cid[:2]/cid


def _read_record(path,cfg):
    with path.open("rb") as stream:
        raw=stream.read(cfg["max_manifest_bytes"]+1)
    if len(raw)>cfg["max_manifest_bytes"]:
        raise ValueError("DOCX evidence manifest exceeds limit")
    record=json.loads(raw)
    if set(record)!={"payload","sha256"} or _sha(_json_bytes(record["payload"]))!=record["sha256"]:
        raise ValueError("DOCX evidence manifest checksum mismatch")
    return record["payload"]


def verify(root,capture_id,*,config):
    cfg=settings(config)
    folder=_folder(root,capture_id)
    payload=_read_record(folder/"manifest.json",cfg)
    identity=payload["identity"]
    if (identity["schema"]!=SCHEMA or _sha(_json_bytes(identity))!=capture_id
            or payload["capture_id"]!=capture_id):
        raise ValueError("DOCX evidence identity mismatch")
    _hash_id(identity["source_docx_sha256"])
    checked={}
    for item in payload["assets"]:
        digest=_hash_id(item["sha256"])
        size=item["bytes"]
        path=folder/"assets"/(digest+".bin")
        if type(size) is not int or not 0<size<=cfg["max_asset_bytes"]:
            raise ValueError("DOCX asset size limit")
        if digest not in checked:
            if path.stat().st_size!=size or digest_file(path)!=digest:
                raise ValueError("DOCX asset checksum/size mismatch")
            checked[digest]=size
        elif checked[digest]!=size:
            raise ValueError("DOCX asset size mismatch")
    return {"capture_id":capture_id,"source_docx_sha256":identity["source_docx_sha256"],
            "story_parts":len(payload["stories"]),"chart_parts":len(payload["charts"]),
            "asset_parts":len(payload["assets"]),"unique_assets":len(checked),
            "retained_asset_bytes":sum(checked.values()),"counts":payload["counts"],
            "unresolved":payload["unresolved"],"local_content_verified":True,
            "durable_storage_verified":False,"purge_safe":False,
            "original_copied":False,"pagination":"NOT_RENDERED"}


def read_evidence(root,capture_id,*,config):
    verify(root,capture_id,config=config)
    return _read_record(_folder(root,capture_id)/"manifest.json",settings(config))


def capture(source,root,*,policy_id,authorized_derivatives,config,expected_source_sha256=None):
    cfg=settings(config)
    if authorized_derivatives is not True:
        raise PermissionError("Explicit DOCX extracted evidence authorization required")
    if not isinstance(policy_id,str) or not re.fullmatch(r"[A-Za-z0-9_.-]{1,100}",policy_id):
        raise ValueError("Explicit retention policy identifier required")
    source=Path(source).resolve(strict=True)
    if source.suffix.lower()!=".docx" or not source.is_file():
        raise ValueError("Expected existing DOCX file")
    started=time.monotonic()
    state=_source_state(source)
    if state[2]>cfg["max_package_bytes"]:
        raise ValueError("DOCX package byte limit exceeded")
    sha=digest_file(source)
    if expected_source_sha256 is not None and sha!=expected_source_sha256:
        raise ValueError('Intake DOCX source version mismatch')
    if _source_state(source)!=state:
        raise RuntimeError("DOCX source changed during hashing")
    identity={"schema":SCHEMA,"source_docx_sha256":sha,"policy_id":policy_id}
    cid=_sha(_json_bytes(identity))
    folder=_folder(root,cid)
    marker=folder/"manifest.json"
    if marker.exists():
        report=verify(root,cid,config=cfg)
        if _source_state(source)!=state or digest_file(source)!=sha:
            raise RuntimeError("DOCX source changed during replay")
        return {**report,"skipped_verified":True,"elapsed_seconds":round(time.monotonic()-started,3)}
    stories,charts,assets,counts,unsupported=[],[],[],{},{}
    unresolved=[]
    with zipfile.ZipFile(source) as z:
        infos=z.infolist()
        names={i.filename for i in infos}
        if (len(infos)>cfg["max_parts"] or len(infos)!=len(names)
                or sum(i.file_size for i in infos)>cfg["max_uncompressed_bytes"]):
            raise ValueError("DOCX ZIP inventory exceeds limits or duplicate part")
        for i in infos:
            n=i.filename
            if (n.startswith("/") or "\\" in n or ".." in n.split("/")
                    or i.flag_bits&1):
                raise ValueError("Unsafe or encrypted DOCX package entry")
        if "[Content_Types].xml" not in names or "word/document.xml" not in names:
            raise ValueError("Unsupported DOCX package structure")
        for part in sorted(names):
            if STORY.fullmatch(part):
                xml=_xml(z,part,cfg)
                if not xml.tag.startswith("{"+W+"}"):
                    raise ValueError("Unsupported WordprocessingML namespace")
                rels=_rels(z,part,names,cfg)
                stories.append({"part":part,"tokens":_project(xml,counts,unsupported),
                                "relationships":rels})
                for rid,rel in rels.items():
                    if rel["external"] or rel["target_part"] is None:
                        unresolved.append({"part":part,"relationship":rid,"kind":rel["kind"],
                                           "reason":"EXTERNAL_OR_UNRESOLVED_RELATIONSHIP"})
            elif CHART.fullmatch(part):
                xml=_xml(z,part,cfg)
                charts.append({"part":part,"tokens":_project(xml,counts,unsupported,chart=True),
                               "status":"CHART_CACHE_ONLY_NOT_RENDERED"})
            elif part.startswith("word/media/") and not part.endswith("/"):
                if Path(part).suffix.lower() not in IMAGE_EXT:
                    unresolved.append({"part":part,"reason":"UNSUPPORTED_MEDIA_TYPE"})
                    continue
                data=_read(z,part,cfg["max_asset_bytes"])
                if not data:
                    raise ValueError("Empty DOCX media asset")
                digest=_sha(data)
                _write_once(folder/"assets"/(digest+".bin"),data)
                assets.append({"part":part,"sha256":digest,"bytes":len(data),
                               "format":Path(part).suffix.lower(),
                               "status":"EXTRACTED_BYTES_NOT_RENDERED"})
            elif part.startswith(("word/embeddings/","word/diagrams/","word/activeX/",
                                  "word/charts/","customXml/","word/vba")) and not part.endswith("/"):
                unresolved.append({"part":part,"reason":"UNSUPPORTED_EMBEDDED_OR_CUSTOM_PART"})
    if _source_state(source)!=state or digest_file(source)!=sha:
        raise RuntimeError("DOCX source changed during extraction")
    payload={"identity":identity,"capture_id":cid,"stories":stories,"charts":charts,
             "assets":assets,"counts":counts,"unrecognized_or_wrapper_tags":unsupported,
             "unresolved":unresolved,"interpretation_status":"NOT_PERFORMED",
             "pagination":"NOT_RENDERED","purge_safe":False}
    encoded=_json_bytes({"payload":payload,"sha256":_sha(_json_bytes(payload))})
    if len(encoded)>cfg["max_manifest_bytes"]:
        raise ValueError("DOCX evidence manifest exceeds limit")
    _write_once(marker,encoded)
    return {**verify(root,cid,config=cfg),"skipped_verified":False,
            "elapsed_seconds":round(time.monotonic()-started,3)}



def _walk_tokens(nodes, prefix=(), ancestors=()):
    """Iterate retained token paths, not XML XPath or rendered coordinates."""
    for index,node in enumerate(nodes):
        path=prefix+(index,)
        yield node,path,ancestors
        yield from _walk_tokens(node.get("children",()),path,ancestors+((node,path),))


_LOGICAL_TEXT={"w:t","a:t","w:instrText","w:delText"}
_LOGICAL_BREAKS={"w:tab":"\t","w:br":"\n","w:cr":"\n",
                 "w:noBreakHyphen":"\u2011","w:softHyphen":"\u00ad"}
_STRUCTURAL={"w:tbl","w:tr","w:tc"}
_REVIEW_CONTEXT={"w:ins","w:del","w:moveFrom","w:moveTo","w:fldSimple",
                 "w:txbxContent","w:pict","w:drawing","w:object"}


def _locator(part,path):
    return {"kind":"docx_retained_token_path","part":part,"token_path":list(path)}


def iter_logical_blocks(root,capture_id,*,config):
    """Yield verified logical source blocks with explicit non-page locators.

    Paragraphs, physical table/row/cell anchors and unscoped text are emitted
    once. Text belonging to nested paragraphs is NOT duplicated in the parent.
    This is an adapter for future common ingestion, not a PDF page surrogate.
    Text segments are evidence, not a claim of final rendered/visible text.
    """
    evidence=read_evidence(root,capture_id,config=config)
    identity=evidence["identity"]
    # Story order is logical only. Main document first; headers, footnotes etc.
    # remain distinct parts, never interleaved into an invented page order.
    stories=sorted(evidence["stories"],key=lambda s:(s["part"]!="word/document.xml",s["part"]))
    for story in stories:
        part=story["part"]
        tokens=story["tokens"]
        for node,path,ancestors in _walk_tokens(tokens):
            tag=node["tag"]
            table_context=[{"tag":a["tag"],"locator":_locator(part,p)}
                           for a,p in ancestors if a["tag"] in _STRUCTURAL]
            review=sorted({a["tag"] for a,_ in ancestors if a["tag"] in _REVIEW_CONTEXT})
            if tag not in _STRUCTURAL|{"w:p"}|_LOGICAL_TEXT|set(_LOGICAL_BREAKS):
                continue
            if tag in _LOGICAL_TEXT or tag in _LOGICAL_BREAKS:
                if any(a["tag"]=="w:p" for a,_ in ancestors):
                    continue
                kind="unscoped_text"
                descendants=[(node,path,ancestors)]
            elif tag=="w:p":
                kind="paragraph"
                # Descendant walk stops at nested paragraph. The outer block
                # cannot claim the same source text as a text-box paragraph.
                def own(children,prefix,parents):
                    for i,child in enumerate(children):
                        child_path=prefix+(i,)
                        if child["tag"]=="w:p":
                            continue
                        yield child,child_path,parents
                        yield from own(child.get("children",()),child_path,parents+((child,child_path),))
                descendants=own(node.get("children",()),path,ancestors+((node,path),))
            else:
                kind={"w:tbl":"table","w:tr":"row","w:tc":"cell"}[tag]
                descendants=()
            segments=[]
            flags=set(review)
            references=[]
            for child,child_path,parents in descendants:
                ct=child["tag"]
                attrs=child.get("attributes",{})
                refs={k:v for k,v in attrs.items() if k.startswith("r:")}
                if refs:
                    references.append({"locator":_locator(part,child_path),
                                       "relationships":refs})
                if ct not in _LOGICAL_TEXT and ct not in _LOGICAL_BREAKS:
                    continue
                child_flags=sorted({a["tag"] for a,_ in parents if a["tag"] in _REVIEW_CONTEXT})
                if ct in ("w:instrText","w:delText","a:t"):
                    child_flags=sorted(set(child_flags)|{ct})
                flags.update(child_flags)
                segments.append({"tag":ct,"text":child.get("text","") if ct in _LOGICAL_TEXT else _LOGICAL_BREAKS[ct],
                                 "locator":_locator(part,child_path),"review_context":child_flags})
            # Never expand merge cells or invent a grid/header. All source
            # geometry/formatting tokens remain in the checked manifest.
            block_locator=_locator(part,path)
            block={"block_id":_sha(_json_bytes({"capture_id":capture_id,"locator":block_locator})),
                   "capture_id":capture_id,"source_docx_sha256":identity["source_docx_sha256"],
                   "locator":block_locator,"kind":kind,"source_tag":tag,
                   "table_ancestry":table_context,"segments":segments,
                   "relationships":references,"review_context":sorted(flags),
                   "pagination":"NOT_RENDERED","source_text_status":"NATIVE_TOKENS_NOT_RENDERED",
                   "source_facts_verified":False}
            if kind in ("table","row","cell"):
                props={"table":("w:tblPr","w:tblGrid"),"row":("w:trPr",),"cell":("w:tcPr",)}[kind]
                block["source_properties"]=[child for child in node.get("children",()) if child["tag"] in props]
            yield block


def logical_summary(root,capture_id,*,config):
    """Bounded counters for logical adapter acceptance; no DB or cloud writes."""
    counts={}
    text_tokens=0
    review_blocks=0
    references=0
    for block in iter_logical_blocks(root,capture_id,config=config):
        kind=block["kind"]
        counts[kind]=counts.get(kind,0)+1
        text_tokens+=sum(s["tag"]=="w:t" for s in block["segments"])
        review_blocks+=bool(block["review_context"])
        references+=len(block["relationships"])
    return {"capture_id":capture_id,"logical_block_counts":counts,"native_text_tokens":text_tokens,
            "review_blocks":review_blocks,"relationship_occurrences":references,
            "pagination":"NOT_RENDERED","purge_safe":False,"database_writes":False}


def main(argv=None):
    parser=argparse.ArgumentParser()
    parser.add_argument("action",choices=("capture","verify","logical-summary"))
    parser.add_argument("--root",required=True)
    parser.add_argument("--profile",required=True)
    parser.add_argument("--file")
    parser.add_argument("--capture-id")
    parser.add_argument("--policy-id")
    parser.add_argument("--authorized-derivatives",action="store_true")
    args=parser.parse_args(argv)
    cfg=_load_profile(args.profile)
    if args.action=="capture":
        if not args.file:
            parser.error("capture requires --file")
        result=capture(args.file,args.root,policy_id=args.policy_id,
                       authorized_derivatives=args.authorized_derivatives,config=cfg)
    else:
        if not args.capture_id:
            parser.error("verify requires --capture-id")
        result=(logical_summary(args.root,args.capture_id,config=cfg)
                if args.action=="logical-summary" else verify(args.root,args.capture_id,config=cfg))
    print(json.dumps(result,ensure_ascii=True,sort_keys=True))


if __name__=="__main__":
    main()
