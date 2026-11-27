"""Conservative cross-page table linkage on already-preserved source cells.

Never modifies source_tables or fabricates missing rows. When a continuation
is ambiguous, it remains separate. Output is lightweight element metadata,
which the existing chunk JSONB metadata column can carry without new tables.
"""
from __future__ import annotations

import hashlib
import re


def _header(element):
    table = getattr(element, "source_table", None) or {}
    cells = table.get("source_cells") or []
    for row in cells[:3]:
        if not isinstance(row, list):
            continue
        values=tuple(re.sub(r"\s+", " ", str(cell or "")).strip().casefold()
                     for cell in row)
        if sum(bool(v) for v in values) >= 2:
            return values
    return ()


def _candidate(prev_page, prev, page, nxt, *, edge_fraction, x_tolerance):
    prev_data=getattr(prev,"source_table",None)
    next_data=getattr(nxt,"source_table",None)
    if not prev_data or not next_data:
        return False
    if prev_data.get("max_columns") != next_data.get("max_columns"):
        return False
    if prev_data.get("max_columns",0) < 2:
        return False
    # Both page-edge positioning AND a repeated source header are required.
    # Similar widths alone can merge unrelated adjacent regulatory tables.
    if (prev.bbox.y1 < prev_page.height * (1-edge_fraction)
            or nxt.bbox.y0 > page.height * edge_fraction):
        return False
    if (_header(prev) != _header(nxt) or not _header(prev)):
        return False
    # A nonempty first row is not proof of a column header. Require an
    # explicitly rendered source-table Markdown separator on BOTH pages.
    # This avoids joining unrelated tables which happen to share first data
    # values and column geometry.
    sep=re.compile(r"(?m)^\|\s*-{3,}")
    if not sep.search(str(prev.text)) or not sep.search(str(nxt.text)):
        return False
    if (abs(prev.bbox.x0/prev_page.width - nxt.bbox.x0/page.width) > x_tolerance
            or abs(prev.bbox.x1/prev_page.width - nxt.bbox.x1/page.width) > x_tolerance):
        return False
    return True


def link_tables(struct, policy):
    mode=policy.get("table_continuation","none")
    if mode not in ("none","conservative"):
        raise ValueError("Unsupported cross-page table policy")
    maximum=policy.get("max_table_pages",8)
    edge=policy.get("table_edge_fraction",0.25)
    tolerance=policy.get("table_x_tolerance",0.05)
    if type(maximum) is not int or not 1<=maximum<=64:
        raise ValueError("Invalid max_table_pages")
    if type(edge) not in (float,int) or not 0.05<=edge<=0.45:
        raise ValueError("Invalid table_edge_fraction")
    if type(tolerance) not in (float,int) or not 0.0<=tolerance<=0.2:
        raise ValueError("Invalid table_x_tolerance")
    pages=sorted(struct.pages,key=lambda p:p.page_number)
    groups=[]
    last_page=None
    last_element=None
    last_group=None
    for page in pages:
        if page.error or page.is_blank:
            last_page=page
            last_element=None
            last_group=None
            continue
        page_tables=[
            el for el in page.elements
            if getattr(getattr(el,"element_type",None),"value","")
               == "table" and getattr(el,"source_table",None)
        ]
        for index,element in enumerate(page_tables):
            merged=False
            if (mode=="conservative" and index==0
                    and last_group is not None and last_element is not None
                    and last_page is not None
                    and page.page_number==last_page.page_number+1
                    and len(last_group)<maximum
                    and _candidate(last_page,last_element,page,element,
                                   edge_fraction=edge,x_tolerance=tolerance)):
                last_group.append((page,element))
                merged=True
            if not merged:
                last_group=[(page,element)]
                groups.append(last_group)
            # Only LAST table on the previous page may continue to FIRST
            # table on the next page.
            last_element=element if index==len(page_tables)-1 else None
        last_page=page
        if not page_tables:
            last_group=None
            last_element=None
    result={}
    for group in groups:
        first_page=group[0][0].page_number
        last_page=group[-1][0].page_number
        group_id=hashlib.sha256(
            f"{struct.doc_id}::table::{first_page}::{group[0][1].reading_order}".encode()
        ).hexdigest()
        pages_in_group=[p.page_number for p,_ in group]
        source_rows=sum(
            len(element.source_table["source_cells"]) for _,element in group
        )
        # Subsequent repeated header rows are retained in original page cells
        # but counted once in the logical table projection.
        effective_rows=source_rows-(len(group)-1 if len(group)>1 else 0)
        for position,(page,element) in enumerate(group):
            info={
                "table_id":group_id,
                "page_start":first_page,
                "page_end":last_page,
                "fragment_index":position,
                "fragment_count":len(group),
                "source_pages":pages_in_group,
                "continuation_verified":len(group)>1,
                "method":"repeated_header_and_page_edge" if len(group)>1 else "single_page",
                "header_cells":list(_header(group[0][1])),
                "source_row_count":source_rows,
                "logical_row_count":effective_rows,
                "column_count":element.source_table["max_columns"],
            }
            element.table_continuity=info
        result[group_id]=[el for _,el in group]
    return result


def iter_logical_table_rows(fragments):
    """Iterate a linked table's original rows in source order, without copying.

    Yield (page_number, source_row_index, original_cell_list) for every
    logical row. Duplicate *verified* continuation header rows are skipped
    only in the derived projection: every original source_table stays intact.
    Caller may stream the iterator into a grounded summary with page citations.
    """
    fragments=list(fragments)
    if not fragments:
        return
    fragments.sort(key=lambda el:getattr(el,"table_continuity",{}).get(
        "fragment_index",-1))
    first=getattr(fragments[0],"table_continuity",{})
    if not first or first.get("fragment_index")!=0:
        raise ValueError("Logical table group has no initial fragment")
    expected=first["fragment_count"]
    if len(fragments)!=expected:
        raise ValueError("Logical table fragment count mismatch")
    header=_header(fragments[0])
    for index,element in enumerate(fragments):
        info=getattr(element,"table_continuity",{})
        if (info.get("table_id")!=first["table_id"]
                or info.get("fragment_index")!=index
                or info.get("fragment_count")!=expected):
            raise ValueError("Logical table fragment provenance mismatch")
        rows=(getattr(element,"source_table",None) or {}).get("source_cells")
        if not isinstance(rows,list):
            raise ValueError("Logical table source cells missing")
        pages=info["source_pages"]
        page_number=pages[index]
        for row_index,row in enumerate(rows):
            if index>0 and row_index==0 and header and _header(element)==header:
                # Only skip a repeated first-row header if the first row
                # itself matches (not merely a matching header lower down).
                first_row=tuple(re.sub(r"\s+"," ",str(cell or "")).strip().casefold()
                                for cell in row)
                if first_row==header:
                    continue
            yield page_number,row_index,row


def extractive_table_summary(fragments):
    """Bounded description of source rows, not an interpretation of trends."""
    import json
    info=fragments[0].table_continuity
    first=last=None
    count=0
    for page,row_index,row in iter_logical_table_rows(fragments):
        count+=1
        # The first row may be a header; never label it as a clinical result.
        encoded=json.dumps(row,ensure_ascii=False,separators=(",",":"))
        sample={"page":page,"row_index":row_index,"cells":row} if len(encoded)<=400 else None
        if count==1:
            first=sample
        last=sample
    samples=[]
    for sample in (first,last):
        if sample is not None and sample not in samples:
            samples.append(sample)
    text=(f"Source table: {info['column_count']} columns and {count} projected rows "
          f"on physical pages {info['page_start']}–{info['page_end']}.")
    for sample in samples:
        text+=(" Source row on page "+str(sample["page"])+", row index "+
               str(sample["row_index"])+": "+
               json.dumps(sample["cells"],ensure_ascii=False,separators=(",",":"))+".")
    return {"table_id":info["table_id"],"status":"EXTRACTIVE_SAMPLES_ONLY",
            "page_start":info["page_start"],"page_end":info["page_end"],
            "projected_rows":count,"columns":info["column_count"],
            "samples":samples,"text":text,
            "limitations":"Representative source rows only; no inferred trends, comparisons or clinical conclusions."}
