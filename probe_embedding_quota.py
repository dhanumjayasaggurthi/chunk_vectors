"""Inspect Azure/enterprise embedding rate-limit headers with a tiny synthetic request.

One call by default. Does not read source tables, mutate source data, or print credentials.
Usage:
    python probe_embedding_quota.py --profile profiles/clinical_1536.json
"""
from __future__ import annotations

import argparse
import configparser
import json
import sys
import time

import requests

HEADERS = (
    "x-ratelimit-limit-requests",
    "x-ratelimit-limit-tokens",
    "x-ratelimit-remaining-requests",
    "x-ratelimit-remaining-tokens",
    "x-ratelimit-reset-requests",
    "x-ratelimit-reset-tokens",
    "retry-after",
    "retry-after-ms",
)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--profile", help="Optional embedding profile JSON")
    parser.add_argument("--config", default="config.ini", help="Existing local INI config")
    parser.add_argument("--samples", type=int, default=1, help="1..3 small requests")
    parser.add_argument("--interval", type=float, default=2.0, help="Seconds between samples")
    args = parser.parse_args()
    if not 1 <= args.samples <= 3:
        parser.error("--samples must be in 1..3")
    if args.interval < 0:
        parser.error("--interval must be >= 0")
    config = configparser.ConfigParser()
    if not config.read(args.config):
        parser.error("Cannot read local config.ini")
    if "AZURE_OPENAI_EMBEDDING" not in config:
        parser.error("Missing [AZURE_OPENAI_EMBEDDING] in config")
    settings = config["AZURE_OPENAI_EMBEDDING"]
    profile = {}
    if args.profile:
        with open(args.profile, encoding="utf-8") as f:
            profile = json.load(f).get("embedding", {})
    deployment = profile.get("deployment", settings["deployment"])
    model = profile.get("model", settings.get("model", deployment))
    dimensions = int(profile.get("dimensions", settings.get("dimensions", 1536)))
    endpoint = (settings["api_base"].rstrip("/")
                + f"/openai/deployments/{deployment}/embeddings"
                + f"?api-version={settings['api_version']}")
    payload = {
        "model": model,
        "input": ["Rate limit diagnostic. This is a synthetic test sentence."],
        "dimensions": dimensions,
    }
    print(f"Model={model}, deployment={deployment}, dimensions={dimensions}")
    print("Sending only synthetic text. Endpoint URL, API key and vectors are not printed.")
    with requests.Session() as session:
        for sample in range(args.samples):
            if sample:
                time.sleep(args.interval)
            try:
                response = session.post(
                    endpoint,
                    headers={"api-key": settings["api_key"], "Content-Type": "application/json"},
                    json=payload, timeout=(10, 30))
            except requests.RequestException as exc:
                print("Request failed:",type(exc).__name__,str(exc)[:200],file=sys.stderr)
                return 2
            print(f"Sample {sample + 1}/{args.samples}: HTTP {response.status_code}")
            observed = {name: response.headers[name] for name in HEADERS
                        if name in response.headers}
            if observed:
                for name, value in observed.items():
                    print(f"  {name}: {value}")
            else:
                print("  No standard Azure rate-limit headers detected.")
            if response.status_code != 200:
                try:
                    err=response.json().get("error",{})
                    print("  Error code:", str(err.get("code","unknown"))[:120])
                except (ValueError,AttributeError,TypeError):
                    pass
                if response.status_code in (401, 403, 404):
                    print("  Check gateway/credentials/deployment; do not load test.")
                    return 1
                if response.status_code == 429:
                    print("  Already throttled; stop probing and wait for quota recovery.")
                    return 1
                print("  Inspect local endpoint/model configuration before proceeding.")
                return 1
            if "x-ratelimit-limit-requests" not in observed or "x-ratelimit-limit-tokens" not in observed:
                print("  Cannot infer exact RPM/TPM from this response.")
    print("Rate-limit headers may reflect the current gateway window, not a guaranteed sustained SLA.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
