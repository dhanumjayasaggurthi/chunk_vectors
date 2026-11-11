"""Read-only project quota inspection using Google's Cloud Quotas API.

Usage:
  ./venv/bin/python scripts/check_gcp_quotas.py
  ./venv/bin/python scripts/check_gcp_quotas.py --project PROJECT_ID --json

Uses Application Default Credentials or GOOGLE_APPLICATION_CREDENTIALS; never
creates, raises, or modifies a quota and never sends an OCR request.
The caller needs cloudquotas.quotas.get on the target project and the Cloud
Quotas API must be enabled; a 403 is NOT a zero-quota finding.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path


SERVICES=("vision.googleapis.com","documentai.googleapis.com")
SCOPES=("https://www.googleapis.com/auth/cloud-platform",)


def read_quotas(project,services=SERVICES):
    import google.auth
    from google.auth.transport.requests import AuthorizedSession
    if project and ("/" in project or len(project)>128):
        raise ValueError("Provide a valid Google Cloud project ID")
    # Match vision_ocr.py: use [GCP] gcp_key_path when an explicit
    # ADC environment variable isn't already configured.
    if not os.environ.get("GOOGLE_APPLICATION_CREDENTIALS"):
        import configparser
        ini=configparser.ConfigParser()
        current=Path.cwd()/"config.ini"
        neighboring=Path(__file__).resolve().parent.parent/"config.ini"
        ini.read(current if current.is_file() else neighboring)
        path=ini.get("GCP","gcp_key_path",fallback="").strip()
        if path and Path(path).is_file():
            os.environ["GOOGLE_APPLICATION_CREDENTIALS"]=path
    credentials, default_project=google.auth.default(scopes=SCOPES)
    project=project or default_project
    if not project:
        raise ValueError("Could not resolve the GCP project: supply --project PROJECT_ID")
    with AuthorizedSession(credentials) as session:
        results={}
        for service in services:
            # Only service names from the fixed list are accepted.
            if service not in SERVICES:
                raise ValueError("Unsupported service")
            url=(f"https://cloudquotas.googleapis.com/v1/projects/{project}/"
                 f"locations/global/services/{service}/quotaInfos")
            results[service]=[]
            page_token=None
            while True:
                params={"pageSize":200}
                if page_token:params["pageToken"]=page_token
                response=session.get(url,params=params,timeout=20)
                if response.status_code!=200:
                    raise RuntimeError(
                        f"{service}: Cloud Quotas API returned HTTP {response.status_code}. "
                        "Check the project ID, API enablement and "
                        "cloudquotas.quotas.get IAM permission."
                    )
                payload=response.json()
                results[service].extend(payload.get("quotaInfos",[]))
                page_token=payload.get("nextPageToken")
                if not page_token:break
        return results


def format_quota(item):
    units=item.get("metricUnit",item.get("unit","—"))
    infos=item.get("dimensionsInfos") or []
    if not infos:
        return str(item.get("quotaId","")),units,"not available", "—"
    parts=[]
    for detail in infos:
        dimensions=detail.get("dimensions") or {}
        value=detail.get("details") or {}
        amount=value.get("quotaValue",value.get("value","not reported"))
        parts.append(f"{dimensions or 'project'}={amount}")
    return str(item.get("quotaId","")),units,"; ".join(parts),str(item.get("metricDisplayName",item.get("metric","—")))


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project",help="Google Cloud PROJECT_ID; defaults to credential project")
    parser.add_argument("--json",action="store_true",help="Print raw quota records")
    args=parser.parse_args(argv)
    try:
        result=read_quotas(args.project)
    except Exception as exc:
        print(f"Quota inspection failed ({type(exc).__name__}): {exc}",file=sys.stderr)
        return 2
    if args.json:
        print(json.dumps(result,indent=2))
        return 0
    for service,items in result.items():
        print(f"\n{service} — {len(items)} applied quota definitions")
        for item in items:
            name,unit,values,metric=format_quota(item)
            print(f"  {name}\n    units={unit} metric={metric}\n    {values}")
    print("\nThese are project-applied quota definitions, not measured usage or "
          "a throughput benchmark. Check Quotas & System Limits for actual utilization.")
    return 0


if __name__=="__main__":
    raise SystemExit(main())
