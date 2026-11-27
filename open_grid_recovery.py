"""Recover column-aligned physical lines below a detected ruled header.

This is not a logical table reconstruction. Original detector cells and
native text remain authoritative. No fill-forward or invented values.
"""
from __future__ import annotations

from bisect import bisect_right
from collections import Counter
import hashlib
import json
import math
from statistics import median

FORMAT = "open_grid_physical_lines_v1"


def _canonical(data):
    return json.dumps(data, sort_keys=True, ensure_ascii=False,
                      separators=(",", ":"), allow_nan=False).encode("utf-8")


def _nonspace(text):
    return Counter(c for c in text if not c.isspace())


def validate_physical_body(data, *, max_json_bytes=8_000_000, header_bbox=None):
    keys={"format", "logical_rows_verified", "header_bbox", "column_boundaries",
          "line_boundaries", "physical_cells", "nonspace_characters", "sha256"}
    if not isinstance(data,dict) or set(data)!=keys or data["format"]!=FORMAT or data["logical_rows_verified"] is not False:
        raise ValueError("Invalid physical table body format")
    xs,ys,box,rows=(data[k] for k in ("column_boundaries","line_boundaries","header_bbox","physical_cells"))
    def finite(values):
        return isinstance(values,list) and all(type(x) in (int,float) and math.isfinite(x) for x in values)
    if (not finite(xs) or not finite(ys) or not finite(box) or len(box)!=4
            or not 4<=len(xs)<=129 or not 2<=len(ys)<=4097
            or any(b<=a for a,b in zip(xs,xs[1:])) or any(b<=a for a,b in zip(ys,ys[1:]))
            or box[0]!=xs[0] or box[2]!=xs[-1] or box[1]>=box[3] or box[3]!=ys[0]
            or not isinstance(rows,list) or len(rows)!=len(ys)-1
            or any(not isinstance(row,list) or len(row)!=len(xs)-1
                   or any(not isinstance(cell,str) for cell in row) for row in rows)):
        raise ValueError("Invalid physical table body geometry")
    if header_bbox is not None and box!=list(header_bbox):
        raise ValueError("Physical body belongs to a different header")
    count=sum(sum(not c.isspace() for c in cell) for row in rows for cell in row)
    if type(data["nonspace_characters"]) is not int or count!=data["nonspace_characters"] or not count:
        raise ValueError("Invalid physical table body character count")
    content={k:v for k,v in data.items() if k!="sha256"}
    if hashlib.sha256(_canonical(content)).hexdigest()!=data["sha256"]:
        raise ValueError("Physical table body checksum mismatch")
    if len(_canonical(data))>max_json_bytes:
        raise ValueError("Physical table body exceeds byte budget")
    return data


def recover_open_grid(page, tables, *, max_json_bytes=8_000_000):
    """Return (status, body). Only accept one shallow, wide detected header.

    A full set of continuous source vertical rules is required. Horizontal
    boundaries are derived from physical text positions, not asserted rules.
    Independently assign original page chars to each cell, then compare the
    entire extracted string and non-whitespace glyph multiset. This checks
    membership and text serialization relative to pdfplumber, not semantics.
    """
    if len(tables)!=1:
        return "NOT_SINGLE_GRID",None
    table=tables[0]
    x0,y0,x1,top=map(float,table.bbox)
    xs=sorted(set(float(x) for cell in table.cells for x in (cell[0],cell[2])))
    if len(xs)<4 or len(xs)>129 or top>page.height*.25 or x1-x0<page.width*.4:
        return "NOT_CANDIDATE",None
    ends=[]
    for x in xs:
        matches=[e['bottom'] for e in page.vertical_edges
                 if abs(e['x0']-x)<=2 and e['top']<=top+2
                 and e['bottom']>=top+page.height*.15]
        if not matches:
            return "NO_CONTINUOUS_COLUMN_RULES",None
        ends.append(max(matches))
    bottom=float(min(ends))
    from pdfplumber.utils import extract_words,extract_text
    region=page.crop((x0,top,x1,bottom))
    # Original unclipped glyph centers are the independent membership source.
    chars_by_column=[[] for _ in xs[:-1]]
    source_chars=[]
    for ch in page.chars:
        cx=(ch['x0']+ch['x1'])/2; cy=(ch['top']+ch['bottom'])/2
        if not (x0<=cx<x1 and top<=cy<bottom):
            continue
        col=bisect_right(xs,cx)-1
        if not 0<=col<len(chars_by_column):
            return "SOURCE_CHAR_OUTSIDE_GRID",None
        source_chars.append(ch)
        if not ch['text'].strip():
            continue
        if (ch['x0']<xs[col]-.5 or ch['x1']>xs[col+1]+.5
                or ch['top']<top-.5 or ch['bottom']>bottom+.5):
            return "GLYPH_CROSSES_COLUMN_OR_BODY",None
        chars_by_column[col].append(ch)
    words=[w for chars in chars_by_column for w in extract_words(chars)]
    if len(words)<10:
        return "INSUFFICIENT_BODY",None
    groups=[]
    for word in sorted(words,key=lambda w:(w['top'],w['x0'])):
        if not groups or word['top']-groups[-1][0]['top']>3:
            groups.append([])
        groups[-1].append(word)
    if len(groups)>4096:
        return "BODY_LINE_BUDGET_EXCEEDED",None
    shift=median(w['height'] for w in words)/2
    ys=[top]+[(max(w['top'] for w in a)+min(w['top'] for w in b))/2+shift
              for a,b in zip(groups,groups[1:])]+[bottom]
    if any(b<=a for a,b in zip(ys,ys[1:])):
        return "AMBIGUOUS_ROW_BOUNDARIES",None
    grids=region.find_tables({'vertical_strategy':'explicit','explicit_vertical_lines':xs,
                       'horizontal_strategy':'explicit','explicit_horizontal_lines':ys,
                       'snap_tolerance':0,'intersection_tolerance':1})
    if len(grids)!=1:
        return "AMBIGUOUS_GRID",None
    matrix=grids[0].extract()
    if len(matrix)!=len(ys)-1 or any(len(row)!=len(xs)-1 for row in matrix):
        return "GRID_DIMENSION_MISMATCH",None
    expected={}
    for ch in source_chars:
        col=bisect_right(xs,(ch['x0']+ch['x1'])/2)-1
        row=bisect_right(ys,(ch['top']+ch['bottom'])/2)-1
        if not (0<=row<len(matrix) and 0<=col<len(xs)-1):
            return "SOURCE_CHAR_OUTSIDE_GRID",None
        expected.setdefault((row,col),[]).append(ch)
    for i,row in enumerate(matrix):
        for j,value in enumerate(row):
            chars=expected.get((i,j),[])
            if not isinstance(value,str) or value!=(extract_text(chars) if chars else ""):
                return "CELL_TEXT_MISMATCH",None
            if _nonspace(value)!=_nonspace(''.join(ch['text'] for ch in chars)):
                return "CELL_CHARACTER_MISMATCH",None
    body={"format":FORMAT,"logical_rows_verified":False,"header_bbox":[x0,y0,x1,top],
          "column_boundaries":xs,"line_boundaries":ys,"physical_cells":matrix,
          "nonspace_characters":sum(sum(not c.isspace() for c in cell) for row in matrix for cell in row)}
    body['sha256']=hashlib.sha256(_canonical(body)).hexdigest()
    if len(_canonical(body))>max_json_bytes:
        return "BODY_BYTE_BUDGET_EXCEEDED",None
    validate_physical_body(body,max_json_bytes=max_json_bytes)
    return "PHYSICAL_LINES_VERIFIED_ONLY",body
