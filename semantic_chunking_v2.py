"""Opt-in semantic v2 chunking: paragraphs, sentences, complete table rows.

Source pages remain immutable. Cross-page table links are conservative and
carry source page references in existing chunk_metadata JSONB, not new tables.
Oversized indivisible text is split at Unicode character boundaries without
changing source characters; every exceptional split is marked in metadata.
"""
from __future__ import annotations

from dataclasses import dataclass, replace
import hashlib
import re

from chunker import Chunk, _make_chunk_id
from configurable_chunking import _heading
from table_continuity import link_tables

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


def _paragraph_units(page, el, headings, known_titles, source_end):
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
                               source_end,tuple(headings),"text")
                    text_lines=[]
                depth,title=matched
                headings[:]=headings[:max(0,depth-1)]+[title]
                yield Unit(title,page.page_number,source_end,
                           tuple(headings),"text")
            else:
                text_lines.append(stripped)
        if text_lines:
            yield Unit(" ".join(text_lines),page.page_number,
                       source_end,tuple(headings),"text")


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


def _units(struct,policy):
    known_titles={t.title.strip().casefold() for t in struct.toc}
    toc_by_page={}
    for item in struct.toc:
        toc_by_page.setdefault(item.page_number,[]).append(item)
    headings=[]
    table_row_numbers={}
    for page in sorted(struct.pages,key=lambda p:p.page_number):
        if page.is_blank or page.error:
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
            yield from _paragraph_units(page,el,headings,known_titles,source_end)


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
    reason="oversized_table_row" if unit.kind=="table" else "oversized_paragraph"
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
        return (item.path,item.table_id, item.kind=="table")

    def render(parts):
        first=parts[0]
        body="\n".join(x.text for x in parts)
        prefix=[]
        if preserve and first.path:
            heading=first.path[-1]
            if not body.casefold().startswith(heading.casefold()):
                prefix.append(heading)
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
                if overflow and not new_context and not part.table_id and overlap:
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
