"""Inspect applied Cloud Vision / Document AI project quotas using the OCR key.

Credential precedence (consistent with vision_ocr.py unless explicitly
overridden): --key-file > --config > GOOGLE_APPLICATION_CREDENTIALS >
config.ini [GCP] gcp_key_path > local gcp_key.json.

This tool only makes read-only GET requests. It never sends OCR data,
enables an API, requests a quota increase, or prints private key contents.

Examples:
    python scripts/check_gcp_quotas.py --check-config
    python scripts/check_gcp_quotas.py --service vision
    python scripts/check_gcp_quotas.py --key-file "C:\\secure\\gcp_key.json"
    python scripts/check_gcp_quotas.py --project PROJECT_ID --json
"""
from __future__ import annotations

import argparse
import configparser
import json
import os
import re
import sys
from pathlib import Path

SERVICES = ("vision.googleapis.com", "documentai.googleapis.com")
SCOPES = ("https://www.googleapis.com/auth/cloud-platform",)
ROOT = Path(__file__).resolve().parents[1]


class QuotaInspectionError(RuntimeError):
    """A configuration, authorization or quota API error."""


def _resolve_file(value, *, base=None):
    candidate = Path(value).expanduser()
    if not candidate.is_absolute():
        candidate = (base or Path.cwd()) / candidate
    candidate = candidate.resolve()
    if not candidate.is_file():
        raise QuotaInspectionError(
            "Configured GCP service-account JSON is missing or is not a file. "
            "Check [GCP] gcp_key_path, --key-file, and "
            "GOOGLE_APPLICATION_CREDENTIALS on this host."
        )
    return candidate


def credential_file(*, key_file=None, config_file=None, environ=None,
                    cwd=None, repo_root=None):
    """Return the explicit key path plus a non-secret source description.

    Unlike merely setting GOOGLE_APPLICATION_CREDENTIALS, the caller will
    construct Credentials FROM THIS FILE, so a stale local ADC cache cannot
    silently replace the OCR identity.
    """
    environ = os.environ if environ is None else environ
    cwd = Path.cwd() if cwd is None else Path(cwd)
    root = ROOT if repo_root is None else Path(repo_root)

    if key_file:
        return _resolve_file(key_file, base=cwd), "--key-file"

    if config_file:
        config_path = _resolve_file(config_file, base=cwd)
    else:
        config_path = next(
            (p for p in (cwd / "config.ini", root / "config.ini") if p.is_file()),
            None,
        )

    # Explicit --config wins; otherwise the OCR runtime prioritizes
    # GOOGLE_APPLICATION_CREDENTIALS over the config.ini setting.
    env_path = environ.get("GOOGLE_APPLICATION_CREDENTIALS", "").strip()
    if env_path and not config_file:
        return _resolve_file(env_path, base=cwd), "GOOGLE_APPLICATION_CREDENTIALS"

    if config_path is not None:
        config = configparser.ConfigParser(interpolation=None)
        config.read(config_path, encoding="utf-8")
        configured_path = config.get("GCP", "gcp_key_path", fallback="").strip()
        if configured_path:
            candidate = Path(configured_path).expanduser()
            if candidate.is_absolute():
                return _resolve_file(candidate), "config.ini [GCP] gcp_key_path"
            for base in (cwd, config_path.parent):
                attempt = base / candidate
                if attempt.is_file():
                    return attempt.resolve(), "config.ini [GCP] gcp_key_path"
            raise QuotaInspectionError(
                "[GCP] gcp_key_path is set but its JSON file cannot be found. "
                "Relative paths are checked from the working directory and "
                "the directory containing config.ini."
            )
        if config_file:
            raise QuotaInspectionError(
                "Specified config.ini does not contain [GCP] gcp_key_path."
            )

    # Useful for installations with the standard key name but no config.ini.
    for candidate in (cwd / "gcp_key.json", root / "gcp_key.json"):
        if candidate.is_file():
            return candidate.resolve(), "gcp_key.json"

    raise QuotaInspectionError(
        "No GCP key was found. Set [GCP] gcp_key_path in config.ini, "
        "GOOGLE_APPLICATION_CREDENTIALS, or pass --key-file."
    )


def resolve_identity(*, key_file=None, config_file=None, project=None,
                     environ=None, cwd=None, repo_root=None):
    """Load explicitly from a JSON service-account key; return safe metadata.

    Does not make a network request and never exposes the private key.
    """
    path, source = credential_file(
        key_file=key_file, config_file=config_file, environ=environ,
        cwd=cwd, repo_root=repo_root,
    )
    from google.oauth2 import service_account
    try:
        credentials = service_account.Credentials.from_service_account_file(
            str(path), scopes=SCOPES
        )
    except (OSError, ValueError, KeyError, TypeError) as exc:
        raise QuotaInspectionError(
            "The configured file could not be loaded as a Google service-account "
            "JSON key. Validate the file with your GCP administrator."
        ) from exc

    key_project = credentials.project_id
    selected_project = project or key_project
    if not selected_project or not re.fullmatch(
        r"[a-zA-Z0-9][a-zA-Z0-9.:-]{0,127}", selected_project
    ):
        raise QuotaInspectionError(
            "No valid project was found in gcp_key.json. "
            "Supply --project PROJECT_ID if the quota project differs."
        )
    return credentials, {
        "credential_source": source,
        "service_account": credentials.service_account_email,
        "key_project": key_project,
        "quota_project": selected_project,
        "project_overridden": bool(project and project != key_project),
    }


def _api_error(response):
    """Return structured HTTP status/reason without exposing response bodies."""
    code = getattr(response, "status_code", "unknown")
    reason = "UNKNOWN"
    try:
        error = response.json().get("error", {})
        reason = error.get("status") or reason
        for detail in error.get("details", []):
            if detail.get("@type", "").endswith("google.rpc.ErrorInfo"):
                reason = detail.get("reason") or reason
                break
    except (ValueError, TypeError, AttributeError):
        pass
    # reason is a bounded machine-readable status, never raw API text.
    reason = re.sub(r"[^A-Za-z0-9_]", "_", str(reason))[:80]
    return f"HTTP {code}, reason={reason}"


def inspect_quotas(credentials, project, services=SERVICES, session_factory=None):
    """Continue inspecting other services if one returns 403 or another error."""
    from google.auth.transport.requests import AuthorizedSession
    if not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9.:-]{0,127}", project):
        raise ValueError("Invalid quota project")
    if any(service not in SERVICES for service in services):
        raise ValueError("Unsupported quota service")
    factory = session_factory or AuthorizedSession
    results, failures = {}, {}
    session = factory(credentials)
    try:
        for service in services:
            items = []
            page_token = None
            try:
                while True:
                    url = (
                        f"https://cloudquotas.googleapis.com/v1/projects/{project}/"
                        f"locations/global/services/{service}/quotaInfos"
                    )
                    params = {"pageSize": 200}
                    if page_token:
                        params["pageToken"] = page_token
                    response = session.get(url, params=params, timeout=20)
                    if response.status_code != 200:
                        raise QuotaInspectionError(_api_error(response))
                    payload = response.json()
                    items.extend(payload.get("quotaInfos", []))
                    page_token = payload.get("nextPageToken")
                    if not page_token:
                        break
                results[service] = items
            except QuotaInspectionError as exc:
                failures[service] = str(exc)
            except Exception as exc:
                failures[service] = f"Request failed ({type(exc).__name__})"
    finally:
        session.close()
    return results, failures


def read_quotas(project=None, services=SERVICES, *, key_file=None, config_file=None):
    """Backwards-compatible caller for quota data; raises on partial errors."""
    credentials, info = resolve_identity(
        key_file=key_file, config_file=config_file, project=project
    )
    result, failures = inspect_quotas(
        credentials, info["quota_project"], services=services
    )
    if failures:
        raise QuotaInspectionError(
            "; ".join(f"{name}: {reason}" for name, reason in failures.items())
        )
    return result


def format_quota(item):
    units = item.get("metricUnit", item.get("unit", "—"))
    infos = item.get("dimensionsInfos") or []
    if not infos:
        return str(item.get("quotaId", "")), units, "not available", "—"
    parts = []
    for detail in infos:
        dimensions = detail.get("dimensions") or {}
        values = detail.get("details") or {}
        amount = values.get("quotaValue", values.get("value", "not reported"))
        parts.append(f"{dimensions or 'project'}={amount}")
    return (
        str(item.get("quotaId", "")), units, "; ".join(parts),
        str(item.get("metricDisplayName", item.get("metric", "—"))),
    )


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--key-file", help="Service-account JSON to use explicitly")
    parser.add_argument("--config", help="Explicit config.ini with [GCP] gcp_key_path")
    parser.add_argument("--project", help="Quota project (defaults to key project_id)")
    parser.add_argument(
        "--service", choices=("vision", "documentai", "both"), default="both"
    )
    parser.add_argument(
        "--check-config", action="store_true",
        help="Read key identity only; no network requests or quota checks"
    )
    parser.add_argument("--json", action="store_true", help="Print quota response as JSON")
    args = parser.parse_args(argv)
    try:
        credentials, info = resolve_identity(
            key_file=args.key_file, config_file=args.config,
            project=args.project
        )
    except Exception as exc:
        # Google SDK errors may include sensitive file paths; keep diagnostics
        # deliberately bounded instead of dumping exception tracebacks.
        if isinstance(exc, QuotaInspectionError):
            print(f"Credential configuration failed: {exc}", file=sys.stderr)
        else:
            print(
                f"Credential configuration failed ({type(exc).__name__}).",
                file=sys.stderr
            )
        return 2

    if not args.json:
        print(f"Credential source: {info['credential_source']}")
        print(f"Service account: {info['service_account']}")
        print(f"Key project: {info['key_project']}")
        print(f"Quota project: {info['quota_project']}")
        if info["project_overridden"]:
            print("NOTE: --project overrides the project in the service-account key.")

    if args.check_config:
        if args.json:
            print(json.dumps(info, indent=2))
        else:
            print("JSON key parsed; API authorization and quotas were not tested.")
        return 0

    services = {
        "vision": (SERVICES[0],),
        "documentai": (SERVICES[1],),
        "both": SERVICES,
    }[args.service]
    results, failures = inspect_quotas(
        credentials, info["quota_project"], services=services
    )
    if args.json:
        print(json.dumps({
            "identity": info, "quota_infos": results, "failures": failures
        }, indent=2))
    else:
        for service, items in results.items():
            print(f"\n{service} — {len(items)} applied quota definitions")
            for item in items:
                name, units, values, metric = format_quota(item)
                print(f"  {name}\n    units={units} metric={metric}\n    {values}")
        for service, reason in failures.items():
            print(f"\n{service}: {reason}", file=sys.stderr)

    if failures:
        print(
            "Quota access failed for one or more services. A working Vision OCR "
            "identity may still lack cloudquotas.quotas.get, or the Cloud Quotas "
            "API may be disabled. Ask an administrator to verify "
            "cloudquotas.googleapis.com and grant the service account "
            "roles/cloudquotas.viewer (read-only) on the target project. "
            "No quota values are inferred from a 403.",
            file=sys.stderr,
        )
        return 2
    if not args.json:
        print(
            "\nThese are applied quotas, not observed throughput. "
            "Check Cloud Monitoring for actual utilization."
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
