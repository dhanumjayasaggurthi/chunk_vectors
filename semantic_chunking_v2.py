"""Opt-in semantic v2 chunking: paragraphs, sentences, complete table rows.

Persisted source pages remain immutable. Cross-page table links are conservative
and carry source page references in existing chunk_metadata JSONB, not new tables.
Oversized indivisible text is split at Unicode character boundaries without
changing source characters; every exceptional split is marked in metadata.
"""
from __future__ import annotations

from dataclasses import dataclass, replace
import hashlib
import re

from chunker import Chunk, _make_chunk_id
from configurable_chunking import _heading
from table_continuity import link_tables, extractive_table_summary

_SENTENCES = re.compile(r"(?<=[.!?。！？।؟।])\s+(?=[^\s])")


@dataclass(frozen=True)
class Unit:
    text: str
    page_start: int
    page_end: int
    path: tuple[str, ...]
    kind: str
    table_id: str = ""
    table_header: str = ""
    table_info: dict | None = None
    split_reason: str = ""
    overlap_copy: bool = False
    page_joined: bool = False
    near_top: bool = False
    near_bottom: bool = False
    physical_body_info: dict | None = None
    physical_line_index: int = 0  # positive=body, negative=detected header row



def _paragraph_units(page, el, headings, known_titles, source_end,
                     edge_fraction=0.20):
    # Page adjacency and missing punctuation alone can merge unrelated
    # regulatory text. Require the source block to touch the page edge.
    top = bool(page.height and el.bbox.y0 <= page.height * edge_fraction)
    bottom = bool(page.height and el.bbox.y1 >= page.height * (1-edge_fraction))
    for paragraph in re.split(r"\n\s*\n", el.text):
        paragraph=paragraph.strip()
        if not paragraph:
            continue
        text_lines=[]
        for line in paragraph.splitlines():
            stripped=line.strip()
            if not stripped:
                continue
            matched=_heading(stripped,known_titles)
            if matched:
                if text_lines:
                    yield Unit(" ".join(text_lines),page.page_number,
                               source_end,tuple(headings),"text",
                               near_top=top,near_bottom=bottom)
                    text_lines=[]
                depth,title=matched
                headings[:]=headings[:max(0,depth-1)]+[title]
                yield Unit(title,page.page_number,source_end,
                           tuple(headings),"text",
                           near_top=top,near_bottom=bottom)
            else:
                text_lines.append(stripped)
        if text_lines:
            yield Unit(" ".join(text_lines),page.page_number,
                       source_end,tuple(headings),"text",
                       near_top=top,near_bottom=bottom)


def _markdown_row(row,columns):
    cells=[str(value if value is not None else "") for value in row]
    cells.extend([""]*max(0,columns-len(cells)))
    # Source JSONB still retains exact null/whitespace/newline/pipe cells.
    cells=[x.replace("|",r"\|").replace("\n"," ").replace("\r"," ") for x in cells]
    return "| " + " | ".join(cells) + " |"


def _has_header(el):
    # Derived Markdown has a separator ONLY when PDF table detector found a
    # header. Do not infer headings from arbitrary first-row cell text.
    return bool(re.search(r"(?m)^\|\s*-{3,}",str(el.text)))


def _physical_body_units(struct,page,element,headings):
    """Source-aligned layout, never a verified logical table projection."""
    from open_grid_recovery import validate_physical_body
    from table_extractor import validate_source_table
    box=element.bbox
    body=validate_physical_body(element.physical_table_body,
                                header_bbox=[box.x0,box.y0,box.x1,box.y1],
                                max_json_bytes=100_000_000)
    source=validate_source_table(element.source_table)
    identity=hashlib.sha256(
        f"{struct.doc_id}:physical:{page.page_number}:{element.reading_order}:{body['sha256']}".encode()
    ).hexdigest()
    info={"body_id":identity,"format":body["format"],"source_page":page.page_number,
          "source_element_reading_order":element.reading_order,
          "source_header_sha256":source["sha256"],"physical_body_sha256":body["sha256"],
          "header_bbox":body["header_bbox"],"column_boundaries":body["column_boundaries"],
          "physical_line_count":len(body["physical_cells"]),
          "detected_header_row_count":len(source["source_cells"]),
          "logical_rows_verified":False}
    for i,row in enumerate(source["source_cells"],1):
        # The header may contain spans and nulls. Do not map its cells to
        # body-column labels or repeat it as a fabricated logical header.
        yield Unit("Detected header row "+str(i)+": "+_markdown_row(row,source["max_columns"]),
                   page.page_number,page.page_number,tuple(headings),"physical_table_body",
                   physical_body_info=info,physical_line_index=-i)
    for i,row in enumerate(body["physical_cells"],1):
        yield Unit("Physical line "+str(i)+": "+_markdown_row(row,len(body["column_boundaries"])-1),
                   page.page_number,page.page_number,tuple(headings),"physical_table_body",
                   physical_body_info=info,physical_line_index=i)


def _units(struct,policy):
    known_titles={t.title.strip().casefold() for t in struct.toc}
    toc_by_page={}
    for item in struct.toc:
        toc_by_page.setdefault(item.page_number,[]).append(item)
    headings=[]
    table_row_numbers={}
    release_payloads=bool(getattr(struct,"release_page_payloads_after_chunking",False))
    for page in sorted(struct.pages,key=lambda p:p.page_number):
        if page.is_blank or page.error:
            if release_payloads:
                page.raw_text=""
                page.elements.clear()
            continue
        toc_entries=toc_by_page.get(page.page_number,[])
        if toc_entries:
            first=toc_entries[0]
            headings=headings[:max(0,first.level-1)]+[first.title]
        source_title=getattr(page,"source_section_title","")
        if source_title:
            headings=[source_title]
        source_end=getattr(page,"source_page_end",page.page_number)
        elements=sorted(
            (e for e in page.elements if e.text or (
                policy.get("table_row_mode","legacy")=="rows"
                and getattr(e,"source_table",None)
            )),key=lambda el:getattr(el,"reading_order",0),
        )
        if not elements and page.raw_text:
            yield Unit(page.raw_text.strip(),page.page_number,source_end,
                       tuple(headings),"text")
        for el in elements:
            kind=getattr(el.element_type,"value",str(el.element_type))
            source=getattr(el,"source_table",None)
            if (kind=="table" and policy.get("physical_table_body_mode","none")=="lines"
                    and getattr(el,"physical_table_body",None) is not None):
                if not source:
                    raise ValueError("Physical table body requires original source grid")
                yield from _physical_body_units(struct,page,el,headings)
                continue
            if kind=="table" and policy.get("table_row_mode","legacy")=="rows" and not source:
                raise ValueError(
                    "table_row_mode=rows requires lossless source_table for each table; "
                    f"page {page.page_number} has no source cell grid"
                )
            if kind=="table" and source and policy.get("table_row_mode","legacy")=="rows":
                info=getattr(el,"table_continuity",None) or {}
                table_id=info.get("table_id") or hashlib.sha256(
                    f"{struct.doc_id}:table:{page.page_number}:{el.reading_order}".encode()
                ).hexdigest()
                rows=source["source_cells"]
                columns=source["max_columns"]
                header=""
                if _has_header(el) and rows:
                    header=_markdown_row(rows[0],columns)
                # Continuations repeat their header in the original page;
                # it is represented by the shared header prefix instead.
                skip_header=bool(
                    rows and info.get("fragment_index",0)>0 and header
                )
                start=1 if header else 0
                if skip_header:
                    start=1
                if table_id not in table_row_numbers:
                    table_row_numbers[table_id]=0
                emitted=0
                for row in rows[start:]:
                    table_row_numbers[table_id]+=1
                    emitted+=1
                    yield Unit(
                        _markdown_row(row,columns),
                        page.page_number,source_end,tuple(headings),"table",
                        table_id,header,info,
                    )
                if emitted==0 and info.get("fragment_index",0)==0 and header:
                    # Header-only source table is still discoverable.
                    yield Unit(header,page.page_number,source_end,
                               tuple(headings),"table",table_id,"",info)
                continue
            if not el.text:
                continue
            if kind!="text":
                yield Unit(el.text.strip(),page.page_number,source_end,
                           tuple(headings),kind)
                continue
            yield from _paragraph_units(
                page,el,headings,known_titles,source_end,
                policy.get("page_join_edge_fraction",0.20),
            )
        if release_payloads:
            # The derived Unit/Chunk has copied every required source string
            # and provenance field. Release the reconstructed page payload as
            # soon as its units have been consumed; immutable database rows are
            # untouched and linked logical-table elements remain referenced by
            # ``links`` until their extractive summaries are complete.
            page.raw_text=""
            page.elements.clear()


def _coalesce_cross_page_paragraphs(units):
    """Conservatively join an interrupted paragraph across adjacent pages.

    Requires same section, adjacent pages, unfinished preceding sentence,
    lowercase continuation, and no intervening table/graphic content. This
    alters only derived RAG text, never the original page text or source runs.
    """
    previous=None
    for current in units:
        if previous is None:
            previous=current
            continue
        compatible=(
            previous.kind=="text" and current.kind=="text"
            and previous.path==current.path
            and previous.page_end+1==current.page_start
            and previous.near_bottom and current.near_top
            and bool(previous.text.strip())
            and bool(current.text.strip())
            and not re.search(r"[.!?;:。！？。؟।]\s*$",previous.text)
            and current.text.strip()[0].islower()
        )
        if compatible:
            joiner="" if previous.text.rstrip().endswith("-") else " "
            previous=replace(
                previous,
                text=previous.text.rstrip()+joiner+current.text.lstrip(),
                page_end=current.page_end,
                page_joined=True,
                near_bottom=current.near_bottom,
            )
        else:
            yield previous
            previous=current
    if previous is not None:
        yield previous


def _sentences(text):
    pieces=[p.strip() for p in _SENTENCES.split(text.strip()) if p.strip()]
    return pieces or [text.strip()]


def _char_split(word, budget, count):
    """Unicode-safe split of an indivisible word, never a lossy token decode."""
    remaining=word
    while remaining:
        if count(remaining)<=budget:
            yield remaining
            break
        lo,hi=1,len(remaining)
        while lo<hi:
            mid=(lo+hi+1)//2
            if count(remaining[:mid])<=budget:
                lo=mid
            else:
                hi=mid-1
        if count(remaining[:lo])>budget:
            raise ValueError("A single Unicode scalar exceeds token budget")
        yield remaining[:lo]
        remaining=remaining[lo:]


def _split_words(text,budget,count):
    current=""
    for word in re.findall(r"\S+\s*",text):
        if count(word)>budget:
            if current.strip():
                yield current.strip()
                current=""
            yield from _char_split(word.strip(),budget,count)
        elif current and count(current+word)>budget:
            yield current.strip()
            current=word
        else:
            current+=word
    if current.strip():
        yield current.strip()


def _split(unit,budget,count):
    if count(unit.text)<=budget:
        return [unit]
    pieces=[]
    buffer=""
    reason=("oversized_physical_line" if unit.physical_body_info else
            "oversized_table_row" if unit.kind=="table" else "oversized_paragraph")
    for sentence in _sentences(unit.text):
        if count(sentence)>budget:
            if buffer:
                pieces.append(replace(unit,text=buffer,split_reason=reason))
                buffer=""
            for item in _split_words(sentence,budget,count):
                pieces.append(replace(unit,text=item,split_reason=reason+"_word_fallback"))
        elif buffer and count(buffer+" "+sentence)>budget:
            pieces.append(replace(unit,text=buffer,split_reason=reason))
            buffer=sentence
        else:
            buffer=(buffer+" "+sentence).strip()
    if buffer:
        pieces.append(replace(unit,text=buffer,split_reason=reason))
    if not pieces:
        raise RuntimeError("Oversized content produced no chunks")
    return pieces


def _pack_short_chunks(chunks, policy, count):
    """Merge compatible small retrieval units, never immutable source grids.

    A packed chunk retains explicit per-component source page/table provenance.
    We do not merge across different section paths, large page gaps, missing
    context across pages, hard token limits or excessive distinct table IDs.
    The separate source PDF/page JSON remains the fidelity authority.
    """
    if policy.get("small_section_policy","keep")!="merge_forward":
        return chunks
    minimum=policy["min_tokens"]
    maximum=policy["max_tokens"]
    max_pages=policy.get("small_chunk_max_pages",2)
    max_tables=policy.get("small_chunk_max_tables",3)
    if not minimum:
        return chunks

    def ids_for(item):
        meta=item.chunk_metadata
        return list(meta.get("table_ids") or
                    ([meta["table_id"]] if meta.get("table_id") else []))

    def text_for(group):
        # Every original chunk is preserved in full, including real table
        # headings and complete rows; only an explicit boundary is inserted.
        return "\n\n".join(chunk.chunk_text for chunk in group)

    def can_add(group, nxt):
        if count(text_for(group))>=minimum:
            return False
        first,last=group[0],group[-1]
        # Do not pack away the explicit physical layout warning/line citations.
        if ("physical_table_body" in nxt.content_types
                or any("physical_table_body" in c.content_types for c in group)):
            return False
        if nxt._section_path!=first._section_path:
            return False
        if (not first._section_path
                and nxt.page_start!=last.page_end):
            # Without a trusted section title, never bridge page boundaries.
            return False
        if nxt.page_start>last.page_end+1:
            return False
        if max(nxt.page_end, *(c.page_end for c in group))-min(
                nxt.page_start, *(c.page_start for c in group))+1>max_pages:
            return False
        distinct={
            table_id for item in group+[nxt]
            for table_id in ids_for(item)
        }
        if len(distinct)>max_tables:
            return False
        return count(text_for(group+[nxt]))<=maximum

    def finish(group):
        if len(group)==1:
            return group[0]
        first=group[0]
        text=text_for(group)
        if count(text)>maximum:
            raise RuntimeError("Short-chunk packing exceeded token budget")
        entries=[]
        table_ids=[]
        table_components=[]
        for child in group:
            meta=child.chunk_metadata
            ids=ids_for(child)
            entries.append({
                "page_start":child.page_start,
                "page_end":child.page_end,
                "content_types":list(child.content_types),
                "table_ids":ids,
                "text_sha256":hashlib.sha256(
                    child.chunk_text.encode("utf-8")).hexdigest(),
            })
            for table_id in ids:
                if table_id not in table_ids:
                    table_ids.append(table_id)
                    table_components.append({
                        "table_id":table_id,
                        "source_pages":meta.get("table_source_pages",[
                            child.page_start]),
                        "fragment_count":meta.get("table_fragment_count",1),
                        "continuation_verified":meta.get(
                            "table_continuation_verified",False),
                        "columns":meta.get("table_columns"),
                        "source_rows":meta.get("table_source_rows"),
                    })
        first.chunk_text=text
        first.page_start=min(c.page_start for c in group)
        first.page_end=max(c.page_end for c in group)
        first.content_types=sorted({
            kind for c in group for kind in c.content_types
        })
        original=first.chunk_metadata
        result={
            "semantic_boundary":"compatible_short_chunk_packing",
            "packing_policy":"merge_forward",
            "source_page_start":first.page_start,
            "source_page_end":first.page_end,
            "source_components":entries,
            "overlap_copied":any(
                c.chunk_metadata.get("overlap_copied",False) for c in group),
            "cross_page_paragraph_joined":any(
                c.chunk_metadata.get("cross_page_paragraph_joined",False)
                for c in group),
        }
        reasons=sorted({
            r for child in group for r in
            child.chunk_metadata.get("exceptional_splits",[])
        })
        if reasons:
            result["exceptional_splits"]=reasons
        if table_ids:
            result["table_ids"]=table_ids
            result["table_components"]=table_components
        if (len(table_ids)==1
                and all(c.content_types==["table"] for c in group)):
            # Only a genuinely single-table chunk may expose the historical
            # singular table_id and its individual structural summary.
            for key,value in original.items():
                if key.startswith("table_"):
                    result[key]=value
            result["table_id"]=table_ids[0]
        first.chunk_metadata=result
        return first

    output=[]
    group=[]
    for chunk in chunks:
        if group and can_add(group,chunk):
            group.append(chunk)
        else:
            if group:
                output.append(finish(group))
            group=[chunk]
    if group:
        output.append(finish(group))
    for index,chunk in enumerate(output):
        chunk.chunk_index=index
        chunk.chunk_id=_make_chunk_id(chunk.doc_id,index)
    return output


def chunk_semantic_v2(struct,policy):
    import tiktoken
    encoding=policy.get("tokenizer","cl100k_base")
    if encoding not in ("cl100k_base","o200k_base"):
        raise ValueError("Unsupported tokenizer")
    tokenizer=tiktoken.get_encoding(encoding)
    maximum=policy["max_tokens"]
    overlap=policy["overlap_tokens"]
    preserve=policy.get("preserve_headers",True)
    if maximum<16 or overlap>=maximum//2:
        raise ValueError("Invalid token budget")
    links=link_tables(struct,policy)
    count=lambda s: len(tokenizer.encode(s,disallowed_special=()))
    chunks=[]
    pending=[]

    def context_key(item):
        return (item.path,item.table_id, item.kind=="table",
                (item.physical_body_info or {}).get("body_id"))

    def render(parts):
        first=parts[0]
        body="\n".join(x.text for x in parts)
        prefix=[]
        if preserve and first.path:
            heading=first.path[-1]
            if not body.casefold().startswith(heading.casefold()):
                prefix.append(heading)
        if first.physical_body_info:
            prefix.append("Physical table layout; columns left to right; logical rows unverified.")
        if first.table_id:
            info=first.table_info or {}
            prefix.append(
                "Source table "+first.table_id[:16]+
                " | pages "+str(info.get("page_start",first.page_start))+
                "-"+str(info.get("page_end",first.page_end))
            )
            if first.table_header:
                prefix.append(first.table_header)
                columns=info.get("column_count",0)
                if type(columns) is int and columns > 0:
                    prefix.append("| " + " | ".join(["---"]*columns) + " |")
        return "\n".join(x for x in prefix if x)+"\n\n"+body if prefix else body

    def emit(parts):
        if not parts:
            return
        value=render(parts)
        if count(value)>maximum:
            raise ValueError("Chunk exceeded configured max_tokens")
        first=parts[0]
        last=parts[-1]
        headers=list(dict.fromkeys(h for p in parts for h in p.path))
        item=Chunk(
            chunk_id=_make_chunk_id(struct.doc_id,len(chunks)),
            doc_id=struct.doc_id,file_name=struct.file_name,
            file_path=struct.file_path,chunk_index=len(chunks),
            chunk_total=0,chunk_level="section" if headers else "page",
            section_title=first.path[-1] if first.path else "",
            page_start=min(x.page_start for x in parts),
            page_end=max(x.page_end for x in parts),
            content_types=sorted({x.kind for x in parts}),
            chunk_text=value,
        )
        item.headers=headers
        table_info=first.table_info or {}
        item.chunk_metadata={
            "semantic_boundary":"sentence_or_structural",
            "source_page_start":item.page_start,
            "source_page_end":item.page_end,
            "overlap_copied":any(x.overlap_copy for x in parts),
            "cross_page_paragraph_joined":any(x.page_joined for x in parts),
        }
        reasons=sorted(set(x.split_reason for x in parts if x.split_reason))
        if reasons:
            item.chunk_metadata["exceptional_splits"]=reasons
        if first.physical_body_info:
            item.chunk_metadata["physical_table_body"]={
                **first.physical_body_info,
                "physical_line_indices":sorted({x.physical_line_index for x in parts
                                                if x.physical_line_index>0}),
                "detected_header_row_indices":sorted({-x.physical_line_index for x in parts
                                                       if x.physical_line_index<0}),
            }
        if first.table_id:
            item.chunk_metadata.update({
                "table_id":first.table_id,
                "table_page_start":table_info.get("page_start",first.page_start),
                "table_page_end":table_info.get("page_end",first.page_end),
                "table_source_pages":table_info.get("source_pages",[first.page_start]),
                "table_fragment_count":table_info.get("fragment_count",1),
                "table_continuation_verified":table_info.get("continuation_verified",False),
                "table_columns":table_info.get("column_count"),
                "table_source_rows":table_info.get("source_row_count"),
                "table_logical_rows":table_info.get("logical_row_count"),
                "table_summary_status":"STRUCTURAL_ONLY",
                "table_structure_summary":(
                    str(table_info.get("column_count","unknown"))+
                    " columns; "+str(table_info.get("logical_row_count","unknown"))+
                    " projected rows; original fragments on pages "+
                    ",".join(map(str,table_info.get("source_pages",[first.page_start])))
                ),
            })
        item._section_path=first.path
        chunks.append(item)

    source_units=_units(struct,policy)
    if policy.get("merge_cross_page_paragraphs",False):
        source_units=_coalesce_cross_page_paragraphs(source_units)
    for unit in source_units:
        if not unit.text:
            continue
        empty=replace(unit,text="")
        budget=maximum-count(render([empty]))-4
        if budget<4:
            raise ValueError("Chunk prefix exceeds token budget")
        for part in _split(unit,budget,count):
            overflow=bool(pending and count(render(pending+[part]))>maximum)
            new_context=bool(pending and context_key(pending[-1])!=context_key(part))
            if pending and (new_context or overflow):
                old=pending
                emit(old)
                pending=[]
                if overflow and not new_context and not part.table_id and not part.physical_body_info and overlap:
                    # Carry a COMPLETE sentence only. Never copy a partial
                    # tokenizer slice or duplicate a table row.
                    tail=_sentences(old[-1].text)[-1]
                    if tail and count(tail)<=overlap:
                        carry=replace(old[-1],text=tail,overlap_copy=True,
                                      split_reason="")
                        if count(render([carry,part]))<=maximum:
                            pending=[carry]
            pending.append(part)
            if count(render(pending))>maximum:
                raise ValueError("Single semantic unit exceeded token budget")
    emit(pending)
    chunks=_pack_short_chunks(chunks,policy,count)
    summary_mode=policy.get("table_summary_mode","structural")
    if summary_mode=="none":
        for chunk in chunks:
            chunk.chunk_metadata.pop("table_summary_status",None)
            chunk.chunk_metadata.pop("table_structure_summary",None)
    elif summary_mode=="extractive":
        # One bounded description per logical group, not one per row chunk.
        # Source text and embedding inputs are unchanged.
        emitted_summaries=set()
        for chunk in chunks:
            meta=chunk.chunk_metadata
            ids=meta.get("table_ids") or ([meta["table_id"]] if "table_id" in meta else [])
            for table_id in ids:
                if table_id in links and table_id not in emitted_summaries:
                    meta.setdefault("table_summaries",[]).append(
                        extractive_table_summary(links[table_id]))
                    emitted_summaries.add(table_id)
    section_key=None
    section_id=""
    section_ordinal=0
    section_chunk_index=0
    for index,chunk in enumerate(chunks):
        key=chunk._section_path
        if key!=section_key:
            section_ordinal+=1
            section_chunk_index=0
            section_key=key
            section_id=hashlib.sha256(
                f"{struct.doc_id}:section:{section_ordinal}:{chunk.page_start}:"
                f"{'/'.join(key)}".encode("utf-8")
            ).hexdigest()
        chunk.chunk_metadata.update({
            "section_id":section_id,
            "section_chunk_index":section_chunk_index,
            "previous_chunk_id":chunks[index-1].chunk_id if index else None,
            "next_chunk_id":chunks[index+1].chunk_id if index+1<len(chunks) else None,
        })
        chunk.chunk_total=len(chunks)
        section_chunk_index+=1
        del chunk._section_path
    return chunks
