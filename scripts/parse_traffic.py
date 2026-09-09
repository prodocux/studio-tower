#!/usr/bin/env python3
"""
Traffic Allocation and Rollback Parser for StudioTower Cloud Run Deployments.
Extracts active traffic allocations, primary serving revision, service URLs, and candidate tag URLs
from Google Cloud Run service JSON specifications with strict fail-closed validation.
"""

from __future__ import annotations

import argparse
import json
import sys
from typing import Any


def parse_traffic_allocations(service_spec: dict[str, Any]) -> str:
    """
    Extracts and validates active traffic allocations formatted as 'rev1=pct1,rev2=pct2'.
    Strictly enforces:
    1. Valid revisionName and integer percentages (0-100).
    2. Active allocations must sum to exactly 100%.
    3. If no active traffic entries exist, falls back to latestReadyRevisionName=100.
    4. Fails closed with ValueError if allocations are missing, invalid, or sum != 100.
    """
    if not isinstance(service_spec, dict):
        raise ValueError(f"Invalid service specification: expected dict, got {type(service_spec).__name__}")

    status = service_spec.get("status")
    if not isinstance(status, dict):
        raise ValueError("Service specification missing valid 'status' dictionary.")

    traffic = status.get("traffic")
    pairs: list[str] = []
    total_pct = 0

    if isinstance(traffic, list) and traffic:
        for idx, entry in enumerate(traffic):
            if not isinstance(entry, dict):
                raise ValueError(f"Traffic entry at index {idx} is not a valid dict.")
            rev = entry.get("revisionName")
            pct = entry.get("percent")

            if pct is not None:
                try:
                    pct_int = int(pct)
                except (ValueError, TypeError):
                    raise ValueError(f"Invalid non-integer traffic percent '{pct}' for revision '{rev}'.")
                if pct_int < 0 or pct_int > 100:
                    raise ValueError(f"Invalid traffic percent {pct_int}% (must be 0-100) for revision '{rev}'.")
                if pct_int > 0:
                    if not rev or not str(rev).strip():
                        raise ValueError(f"Traffic entry has {pct_int}% traffic but missing revisionName.")
                    pairs.append(f"{rev}={pct_int}")
                    total_pct += pct_int

    if pairs:
        if total_pct != 100:
            raise ValueError(f"Active traffic allocations do not sum to 100% (got {total_pct}%: {pairs}).")
        return ",".join(pairs)

    latest = status.get("latestReadyRevisionName")
    if latest and str(latest).strip():
        return f"{str(latest).strip()}=100"

    raise ValueError("Invalid service specification: neither active traffic allocations nor latestReadyRevisionName found.")


def parse_primary_revision(service_spec: dict[str, Any]) -> str:
    """
    Identifies the primary revision carrying the majority or first share of production traffic.
    Fails closed if no valid serving or ready revision can be resolved.
    """
    if not isinstance(service_spec, dict):
        raise ValueError("Invalid service specification.")

    status = service_spec.get("status")
    if not isinstance(status, dict):
        raise ValueError("Service specification missing 'status'.")

    traffic = status.get("traffic", []) or []
    if isinstance(traffic, list):
        # 1. Prefer revision with >= 50% traffic
        for entry in traffic:
            if isinstance(entry, dict):
                rev = entry.get("revisionName")
                pct = entry.get("percent", 0)
                try:
                    pct_int = int(pct)
                except (ValueError, TypeError):
                    pct_int = 0
                if rev and pct_int >= 50:
                    return str(rev).strip()

        # 2. Fall back to first revision with > 0% traffic
        for entry in traffic:
            if isinstance(entry, dict):
                rev = entry.get("revisionName")
                pct = entry.get("percent", 0)
                try:
                    pct_int = int(pct)
                except (ValueError, TypeError):
                    pct_int = 0
                if rev and pct_int > 0:
                    return str(rev).strip()

    # 3. Fall back to latest ready revision
    latest = status.get("latestReadyRevisionName")
    if latest and str(latest).strip():
        return str(latest).strip()

    raise ValueError("No serving or ready revision found in service specification.")


def parse_candidate_url(service_spec: dict[str, Any], candidate_tag: str) -> str:
    """
    Finds the dedicated tag URL for a candidate revision from status.traffic.
    Fails closed if the tag URL is not found or is not a valid HTTPS URL.
    """
    if not candidate_tag or not candidate_tag.strip():
        raise ValueError("Candidate tag name must not be empty.")

    if not isinstance(service_spec, dict):
        raise ValueError("Invalid service specification.")

    status = service_spec.get("status")
    if not isinstance(status, dict):
        raise ValueError("Service specification missing 'status'.")

    traffic = status.get("traffic", []) or []
    if isinstance(traffic, list):
        for entry in traffic:
            if isinstance(entry, dict) and entry.get("tag") == candidate_tag:
                url = entry.get("url")
                if url and isinstance(url, str) and url.startswith("https://"):
                    return url.strip()

    raise ValueError(f"Candidate tag '{candidate_tag}' URL not found in Cloud Run traffic status or not HTTPS.")


def parse_service_url(service_spec: dict[str, Any]) -> str:
    """
    Extracts authoritative service URL from status.url.
    Fails closed if missing or not HTTPS.
    """
    if not isinstance(service_spec, dict):
        raise ValueError("Invalid service specification.")

    status = service_spec.get("status")
    if not isinstance(status, dict):
        raise ValueError("Service specification missing 'status'.")

    url = status.get("url")
    if url and isinstance(url, str) and url.startswith("https://"):
        return url.strip()

    raise ValueError("Authoritative Cloud Run service URL (status.url) not found or not HTTPS.")


def build_rollback_command(
    service_name: str,
    project_id: str,
    region: str,
    traffic_alloc: str,
    primary_rev: str = "",
) -> list[str]:
    """
    Builds the exact gcloud run services update-traffic arguments for rollback.
    """
    target = traffic_alloc if traffic_alloc else (f"{primary_rev}=100" if primary_rev else "")
    if not target:
        raise ValueError("Cannot build rollback command without target revision or allocation.")

    return [
        "gcloud",
        "run",
        "services",
        "update-traffic",
        service_name,
        f"--project={project_id}",
        f"--region={region}",
        f"--to-revisions={target}",
    ]


# Built-in Representative Fixtures for Testing & DryRun
FIXTURE_100_SINGLE = {
    "status": {
        "url": "https://studio-tower-api-prod-uc.a.run.app",
        "traffic": [
            {"revisionName": "studio-tower-api-prod-001", "percent": 100}
        ],
        "latestReadyRevisionName": "studio-tower-api-prod-001",
    }
}

FIXTURE_50_50_SPLIT = {
    "status": {
        "url": "https://studio-tower-api-prod-uc.a.run.app",
        "traffic": [
            {"revisionName": "studio-tower-api-blue", "percent": 50},
            {"revisionName": "studio-tower-api-green", "percent": 50},
        ],
        "latestReadyRevisionName": "studio-tower-api-green",
    }
}

FIXTURE_90_10_CANARY = {
    "status": {
        "url": "https://studio-tower-api-prod-uc.a.run.app",
        "traffic": [
            {"revisionName": "studio-tower-api-stable", "percent": 90},
            {"revisionName": "studio-tower-api-canary", "percent": 10},
        ],
        "latestReadyRevisionName": "studio-tower-api-canary",
    }
}

FIXTURE_EMPTY_TRAFFIC = {
    "status": {
        "url": "https://studio-tower-api-prod-uc.a.run.app",
        "traffic": [],
        "latestReadyRevisionName": "studio-tower-api-initial",
    }
}

FIXTURE_WITH_CANDIDATE = {
    "status": {
        "url": "https://studio-tower-api-prod-uc.a.run.app",
        "traffic": [
            {"revisionName": "studio-tower-api-prod-001", "percent": 100},
            {"tag": "candidate-20260908", "url": "https://candidate-20260908---studio-tower-api-prod-uc.a.run.app", "percent": 0},
        ],
        "latestReadyRevisionName": "studio-tower-api-prod-001",
    }
}


def verify_fixtures() -> bool:
    """
    Verifies representative fixtures against traffic parser and command builder.
    Includes both positive behavior tests and negative boundary failure tests.
    """
    # 1. Test 100% single revision
    alloc_100 = parse_traffic_allocations(FIXTURE_100_SINGLE)
    prim_100 = parse_primary_revision(FIXTURE_100_SINGLE)
    svc_url = parse_service_url(FIXTURE_100_SINGLE)
    assert alloc_100 == "studio-tower-api-prod-001=100", f"Expected 100% single, got: {alloc_100}"
    assert prim_100 == "studio-tower-api-prod-001", f"Expected prim prod-001, got: {prim_100}"
    assert svc_url == "https://studio-tower-api-prod-uc.a.run.app"
    cmd_100 = build_rollback_command("api", "proj", "us-central1", alloc_100)
    assert "--to-revisions=studio-tower-api-prod-001=100" in cmd_100

    # 2. Test 50/50 split
    alloc_5050 = parse_traffic_allocations(FIXTURE_50_50_SPLIT)
    prim_5050 = parse_primary_revision(FIXTURE_50_50_SPLIT)
    assert alloc_5050 == "studio-tower-api-blue=50,studio-tower-api-green=50", f"Expected 50/50, got: {alloc_5050}"
    assert prim_5050 == "studio-tower-api-blue", f"Expected blue for 50/50, got: {prim_5050}"
    cmd_5050 = build_rollback_command("api", "proj", "us-central1", alloc_5050)
    assert "--to-revisions=studio-tower-api-blue=50,studio-tower-api-green=50" in cmd_5050

    # 3. Test 90/10 canary
    alloc_9010 = parse_traffic_allocations(FIXTURE_90_10_CANARY)
    prim_9010 = parse_primary_revision(FIXTURE_90_10_CANARY)
    assert alloc_9010 == "studio-tower-api-stable=90,studio-tower-api-canary=10", f"Expected 90/10, got: {alloc_9010}"
    assert prim_9010 == "studio-tower-api-stable", f"Expected stable for 90/10, got: {prim_9010}"
    cmd_9010 = build_rollback_command("api", "proj", "us-central1", alloc_9010)
    assert "--to-revisions=studio-tower-api-stable=90,studio-tower-api-canary=10" in cmd_9010

    # 4. Test empty traffic with latest ready fallback
    alloc_empty = parse_traffic_allocations(FIXTURE_EMPTY_TRAFFIC)
    prim_empty = parse_primary_revision(FIXTURE_EMPTY_TRAFFIC)
    assert alloc_empty == "studio-tower-api-initial=100", f"Expected fallback 100%, got: {alloc_empty}"
    assert prim_empty == "studio-tower-api-initial", f"Expected fallback initial, got: {prim_empty}"

    # 5. Test candidate tag URL extraction
    cand_url = parse_candidate_url(FIXTURE_WITH_CANDIDATE, "candidate-20260908")
    assert cand_url == "https://candidate-20260908---studio-tower-api-prod-uc.a.run.app"

    # 6. Negative Tests: Fails closed on invalid configurations
    # Negative A: Traffic sum != 100% (e.g. 70%)
    bad_sum_fixture = {
        "status": {
            "traffic": [{"revisionName": "rev-a", "percent": 70}],
            "latestReadyRevisionName": "rev-a",
        }
    }
    try:
        parse_traffic_allocations(bad_sum_fixture)
        raise AssertionError("Expected ValueError on traffic sum != 100%")
    except ValueError:
        pass

    # Negative B: Missing candidate tag
    try:
        parse_candidate_url(FIXTURE_WITH_CANDIDATE, "nonexistent-tag")
        raise AssertionError("Expected ValueError on missing candidate tag")
    except ValueError:
        pass

    # Negative C: Empty status without latestReadyRevisionName
    try:
        parse_traffic_allocations({"status": {}})
        raise AssertionError("Expected ValueError on empty status")
    except ValueError:
        pass

    return True


def main() -> None:
    parser = argparse.ArgumentParser(description="Parse Cloud Run traffic allocations.")
    parser.add_argument("--allocations", action="store_true", help="Print traffic allocations string.")
    parser.add_argument("--primary", action="store_true", help="Print primary serving revision name.")
    parser.add_argument("--service-url", action="store_true", help="Extract authoritative status.url.")
    parser.add_argument("--candidate-url", type=str, metavar="TAG", help="Extract URL for candidate tag.")
    parser.add_argument("--allow-empty", action="store_true", help="Allow empty stdin/file (exit 0 with no output).")
    parser.add_argument("--verify-fixtures", action="store_true", help="Run fixture tests on traffic parser.")
    parser.add_argument("json_file", nargs="?", default="-", help="Input JSON file or '-' for stdin.")

    args = parser.parse_args()

    if args.verify_fixtures:
        try:
            verify_fixtures()
            print("PASS: Verified fixtures (100% single, 50/50 split, 90/10 canary, empty traffic fallback, candidate URL, negative bounds).")
            sys.exit(0)
        except Exception as exc:
            print(f"FAIL: Fixture verification failed: {exc}", file=sys.stderr)
            sys.exit(1)

    # Read JSON
    try:
        if args.json_file == "-":
            raw = sys.stdin.read()
        else:
            with open(args.json_file, "r", encoding="utf-8") as f:
                raw = f.read()
    except Exception as exc:
        print(f"ERROR: Failed to read input: {exc}", file=sys.stderr)
        sys.exit(1)

    if not raw.strip():
        if args.allow_empty:
            sys.exit(0)
        print("ERROR: Empty JSON input received by traffic parser.", file=sys.stderr)
        sys.exit(1)

    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        print(f"ERROR: Invalid JSON input to traffic parser: {exc}", file=sys.stderr)
        sys.exit(1)

    try:
        if args.allocations:
            print(parse_traffic_allocations(data))
        elif args.primary:
            print(parse_primary_revision(data))
        elif args.service_url:
            print(parse_service_url(data))
        elif args.candidate_url:
            print(parse_candidate_url(data, args.candidate_url))
        else:
            print(parse_traffic_allocations(data))
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
