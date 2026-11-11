"""Versioned, deterministic, layout-aware token chunking.

This module never calls an LLM, truncates source text, or writes to a
production table. Geometry and source element IDs are carried with chunks.
Cross-page merges require an explicit continuation link from the layout stage;
page adjacency alone is not sufficient proof of semantic continuation.
"""
from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, replace

import tiktoken


@dataclass(frozen=True)
class ChunkPolicy:
    target_tokens: int = 600
    max_tokens: int = 800
    min_tokens: int = 100
    overlap_tokens: int = 80
    tokenizer: str = "cl100k_base"
    include_headings: bool = True
    split_oversized_tables: bool = True

    def __post_init__(self):
        if self.tokenizer not in ("cl100k_base", "o200k_base"):
            raise ValueError("Unsupported tokenizer")
        if not 32 <= self.max_tokens <= 8191:
            raise ValueError("max_tokens must be 32..8191")
        if not 1 <= self.min_tokens <= self.target_tokens <= self.max_tokens:
            raise ValueError("Require 1 <= min_tokens <= target_tokens <= max_tokens")
        if not 0 <= self.overlap_tokens < self.max_tokens // 4:
            raise ValueError("overlap_tokens must be below max_tokens/4")


@dataclass(frozen=True)
class LayoutElement:
    element_id: str
    page: int
    kind: str
    text: str
    bbox: tuple[float, float, float, float] | None = None
    headings: tuple[str, ...] = ()
    continuation_of: str | None = None

    def __post_init__(self):
        if not self.element_id or self.page < 1:
            raise ValueError("A source element needs an ID and a 1-based page")
        if self.kind not in ("paragraph","text","heading","table","image","chart","list"):
            raise ValueError("Unsupported element type")
        if self.bbox is not None and (len(self.bbox)!=4 or
             self.bbox[2]<self.bbox[0] or self.bbox[3]<self.bbox[1]):
            raise ValueError("Invalid bbox")


@dataclass(frozen=True)
class ProducedChunk:
    chunk_id: str
    chunk_index: int
    text: str
    token_count: int
    element_ids: tuple[str, ...]
    pages: tuple[int, ...]
    boxes: tuple[dict, ...]
    content_types: tuple[str, ...]
    heading_path: tuple[str, ...]
    split_element: bool = False
    overlap_from_previous: bool = False
    overlap_element_ids: tuple[str, ...] = ()


@dataclass
class _Unit:
    text: str
    elements: list[LayoutElement]
    split: bool = False


def _measure(enc, text):
    return len(enc.encode(text, disallowed_special=()))


def _split_text(enc, text, limit):
    """Slice without discarding characters; prioritize whitespace boundaries."""
    remaining = text
    while remaining:
        if _measure(enc, remaining) <= limit:
            yield remaining
            break
        lo, hi, best = 1, len(remaining), 0
        while lo <= hi:
            mid=(lo+hi)//2
            if _measure(enc, remaining[:mid]) <= limit:
                best=mid
                lo=mid+1
            else:
                hi=mid-1
        if not best:
            raise ValueError("Token budget cannot fit even one character")
        # Take a nearby space boundary when possible, preserving separators.
        whitespace=max(remaining.rfind(" ",0,best+1),
                       remaining.rfind("\n",0,best+1))
        if whitespace>=max(1,best//2):
            best=whitespace+1
        yield remaining[:best]
        remaining=remaining[best:]


def _heading_prefix(elements, policy):
    if not policy.include_headings or not elements or not elements[0].headings:
        return ""
    return " / ".join(elements[0].headings)+"\n\n"


def _split_unit(unit,enc,policy):
    """Split only oversized units; tables try whole-row boundaries first."""
    prefix=_heading_prefix(unit.elements,policy)
    budget=policy.max_tokens-_measure(enc,prefix)
    if budget<8:
        raise ValueError("Heading consumes the entire chunk budget")
    if _measure(enc,prefix+unit.text)<=policy.max_tokens:
        return [unit]
    if unit.elements[0].kind=="table" and not policy.split_oversized_tables:
        raise ValueError("Table exceeds token budget with splitting disabled")

    parts=[]
    text=unit.text
    if unit.elements[0].kind=="table" and "\n" in text:
        # Preserve table rows and repeat the Markdown header/separator when
        # feasible. The original lines remain in the first fragment only;
        # repeated context is explicitly duplicated in later fragments.
        lines=text.splitlines(keepends=True)
        if len(lines)>2 and lines[0].lstrip().startswith("|"):
            header="".join(lines[:2])
            tail=lines[2:]
            group=""
            for line in tail:
                candidate=group+line
                if group and _measure(enc,header+candidate)>budget:
                    parts.append(header+group)
                    group=""
                if _measure(enc,header+line)>budget:
                    if group:
                        parts.append(header+group)
                        group=""
                    parts.extend(_split_text(enc,line,budget))
                else:
                    group+=line
            if group:parts.append(header+group)
            if parts and all(_measure(enc,prefix+p)<=policy.max_tokens for p in parts):
                return [_Unit(p,list(unit.elements),True) for p in parts]
    # Tokenization is not strictly additive at boundaries. Validate the
    # EXACT rendered heading+text size, recursively reducing fragments if
    # the join produces more tokens than the separate estimates.
    queue=list(_split_text(enc,text,budget))
    if not queue:
        raise ValueError("Cannot create empty chunks from an oversized element")
    good=[]
    while queue:
        part=queue.pop(0)
        if _measure(enc,prefix+part)<=policy.max_tokens:
            good.append(_Unit(part,list(unit.elements),True))
            continue
        tokens=_measure(enc,part)
        narrower=min(budget-4,tokens-1)
        if narrower<1:
            raise ValueError("Heading and element cannot fit the hard token budget")
        subdivisions=list(_split_text(enc,part,narrower))
        if len(subdivisions)<2:
            raise ValueError("Cannot split oversized element without truncation")
        queue=subdivisions+queue
    return good


def _merge_explicit_continuations(elements):
    """Stitch only adjacent linked paragraph fragments, including across pages."""
    out=[]
    for el in elements:
        if (out and el.continuation_of and el.continuation_of==out[-1].elements[-1].element_id
                and el.kind in ("paragraph","text")
                and out[-1].elements[-1].kind in ("paragraph","text")):
            out[-1].text+=el.text
            out[-1].elements.append(el)
        elif el.text:
            out.append(_Unit(el.text,[el]))
    return out


def chunk_elements(doc_id, elements, policy=None, profile_id="default"):
    """Convert ordered page elements into bounded chunks with geometry references.

    Source input order is the layout reading order (not re-sorted here).
    Allows a paragraph to span page ranges if its continuation is explicitly
    identified. Each chunk retains every original geometry reference.
    """
    if not doc_id or not profile_id:
        raise ValueError("doc_id and profile_id are required")
    policy=policy or ChunkPolicy()
    enc=tiktoken.get_encoding(policy.tokenizer)
    units=[]
    for unit in _merge_explicit_continuations(list(elements)):
        units.extend(_split_unit(unit,enc,policy))
    result=[]
    pending=[]
    def emit():
        nonlocal pending
        if not pending:
            return
        group=pending
        first=group[0].elements
        prefix=_heading_prefix(first,policy)
        body="\n".join(u.text for u in group)
        value=prefix+body
        count=_measure(enc,value)
        if count>policy.max_tokens:
            raise ValueError("Internal invariant: emitted chunk above hard limit")
        flattened=[e for u in group for e in u.elements]
        distinct={e.element_id:e for e in flattened}
        refs=list(distinct.values())
        index=len(result)
        chunk_id=hashlib.sha256(
            (doc_id+"\x00"+profile_id+"\x00"+str(index)).encode("utf-8")
        ).hexdigest()
        result.append(ProducedChunk(
            chunk_id=chunk_id,chunk_index=index,text=value,token_count=count,
            element_ids=tuple(distinct),pages=tuple(sorted({e.page for e in refs})),
            boxes=tuple({"element_id":e.element_id,"page":e.page,"bbox":e.bbox}
                       for e in refs if e.bbox is not None),
            content_types=tuple(sorted({e.kind for e in refs})),
            heading_path=first[0].headings if first else (),
            split_element=any(u.split for u in group),
        ))
        pending=[]
    for unit in units:
        prefix=_heading_prefix(unit.elements,policy)
        if pending:
            base_prefix=_heading_prefix(pending[0].elements,policy)
            candidate=base_prefix+"\n".join([u.text for u in pending]+[unit.text])
            is_section_change=unit.elements[0].headings!=pending[-1].elements[-1].headings
            is_table=(unit.elements[0].kind=="table"
                      or pending[-1].elements[-1].kind=="table")
            if (_measure(enc,candidate)>policy.max_tokens
                or (is_section_change and _measure(enc,
                    base_prefix+"\n".join(u.text for u in pending))>=policy.min_tokens)
                or (is_table and _measure(enc,
                    base_prefix+"\n".join(u.text for u in pending))>=policy.target_tokens)):
                emit()
        # Avoid putting a heading from one structural context into another.
        pending.append(unit)
        actual=_heading_prefix(pending[0].elements,policy)+"\n".join(u.text for u in pending)
        if _measure(enc,actual)>policy.max_tokens:
            raise ValueError("Internal invariant: unit too large for chunk")
        if _measure(enc,actual)>=policy.target_tokens:
            emit()
    emit()
    # Context overlap is best-effort: never exceed the hard token limit.
    # Cross-table context is omitted rather than duplicating structured rows.
    # Previous source IDs are tracked separately from this chunk's own elements.
    if policy.overlap_tokens and len(result)>1:
        output=[result[0]]
        for index in range(1,len(result)):
            prior=result[index-1]
            current=result[index]
            if "table" in prior.content_types or "table" in current.content_types:
                output.append(current)
                continue
            ids=enc.encode(prior.text,disallowed_special=())
            tail=enc.decode(ids[-policy.overlap_tokens:])
            prefix="[Previous context]\\n"+tail+"\\n\\n"
            merged=prefix+current.text
            count=_measure(enc,merged)
            if count<=policy.max_tokens:
                output.append(replace(
                    current,text=merged,token_count=count,
                    overlap_from_previous=True,
                    overlap_element_ids=prior.element_ids,
                ))
            else:
                output.append(current)
        result=output
    return result
