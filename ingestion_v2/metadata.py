"""Typed, evidence-backed, optional document metadata.

Field examples are schema guidance only. They NEVER populate missing values.
No model calls or database changes. This deterministic pass may be upgraded to
a separate quota-controlled LLM fallback after an evaluated pilot.
"""
from __future__ import annotations

import datetime as dt
import re
from typing import Iterable


VALID_TYPES=frozenset(("string","integer","number","boolean","date","enum"))
VALID_METHODS=frozenset(("label","regex"))


def validate_metadata_policy(config):
    if not isinstance(config,dict):
        raise ValueError("metadata must be an object")
    if not isinstance(config.get("enabled",False),bool):
        raise ValueError("metadata.enabled must be bool")
    fields=config.get("fields",{})
    if not isinstance(fields,dict) or len(fields)>100:
        raise ValueError("metadata.fields must contain at most 100 fields")
    for name,rule in fields.items():
        if not re.fullmatch(r"[a-z][a-z0-9_]{0,63}",name):
            raise ValueError("Invalid field name")
        if not isinstance(rule,dict):
            raise ValueError(f"{name} must be a field definition")
        if set(rule)-{"enabled","type","method","label","pattern","sample_values",
                      "first_pages","required","allowed_values"}:
            raise ValueError(f"Unknown metadata options for {name}")
        if not isinstance(rule.get("enabled",True),bool):
            raise ValueError(f"enabled for {name} must be bool")
        if rule.get("type") not in VALID_TYPES or rule.get("method") not in VALID_METHODS:
            raise ValueError(f"Invalid type/method for {name}")
        if not isinstance(rule.get("required",False),bool):
            raise ValueError(f"required for {name} must be bool")
        if not 1 <= rule.get("first_pages",10) <= 10000:
            raise ValueError(f"first_pages for {name} must be 1..10000")
        if rule["method"]=="label":
            if not isinstance(rule.get("label"),str) or not rule["label"].strip():
                raise ValueError(f"Missing label for {name}")
        else:
            pattern=rule.get("pattern")
            if not isinstance(pattern,str) or not 1<=len(pattern)<=300:
                raise ValueError(f"Invalid regex for {name}")
            re.compile(pattern)
        samples=rule.get("sample_values",[])
        if not isinstance(samples,list) or len(samples)>20:
            raise ValueError(f"sample_values for {name} must be a list up to 20")
        allowed=rule.get("allowed_values",[])
        if rule["type"]=="enum" and (not isinstance(allowed,list) or not allowed):
            raise ValueError(f"Enum {name} needs allowed_values")
    return config


def _convert(raw,field_type,allowed):
    value=raw.strip()
    if not value:
        raise ValueError("Empty extracted value")
    if field_type=="string":return value
    if field_type=="integer":return int(value.replace(",",""))
    if field_type=="number":return float(value.replace(",",""))
    if field_type=="boolean":
        if value.casefold() in {"yes","true","1"}:return True
        if value.casefold() in {"no","false","0"}:return False
        raise ValueError("Boolean value not recognized")
    if field_type=="date":
        return dt.date.fromisoformat(value).isoformat()
    if field_type=="enum":
        hits=[item for item in allowed if str(item).casefold()==value.casefold()]
        if not hits:raise ValueError("Not an allowed enum value")
        return hits[0]
    raise ValueError("Unsupported metadata type")


def extract_metadata(elements,config):
    """Return one result per requested field, including missing/invalid.

    Each found value cites the exact source page, source element and bbox.
    No cross-document inference or hypothetical values.
    """
    validate_metadata_policy(config)
    if not config.get("enabled",False):
        return {}
    results={}
    for name,rule in config.get("fields",{}).items():
        if not rule.get("enabled",True):
            results[name]={"status":"DISABLED","value":None}
            continue
        method=rule["method"]
        if method=="label":
            expression=re.compile(
                r"(?im)^\s*"+re.escape(rule["label"])+r"\s*[:#\-]\s*(.+?)\s*$"
            )
        else:
            expression=re.compile(rule["pattern"],re.MULTILINE)
        found=None
        errors=[]
        for element in elements:
            page=getattr(element,"page",None)
            if page is None or page>rule.get("first_pages",10):
                continue
            text=getattr(element,"text","")
            if not isinstance(text,str):
                continue
            # Apply trusted regex only to bounded text per element.
            match=expression.search(text[:20000])
            if not match:
                continue
            raw=match.group(1) if match.lastindex else match.group(0)
            try:
                value=_convert(raw,rule["type"],rule.get("allowed_values",[]))
            except (ValueError,OverflowError) as err:
                errors.append(type(err).__name__)
                continue
            found={
                "status":"FOUND","value":value,
                "method":method,
                "evidence":{
                    "text":raw[:300],"page":page,
                    "element_id":getattr(element,"element_id",None),
                    "bbox":getattr(element,"bbox",None),
                },
                # Do not invent probabilistic confidence for regex.
                "confidence":None,
            }
            break
        if found:
            results[name]=found
        else:
            results[name]={
                "status":"INVALID" if errors else "NOT_FOUND",
                "value":None,"required":rule.get("required",False),
                "validation_errors":errors[:5],
            }
    return results
