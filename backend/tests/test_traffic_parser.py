import pytest
from scripts.parse_traffic import (
    parse_traffic_allocations,
    parse_primary_revision,
    parse_candidate_url,
    parse_service_url,
    build_rollback_command,
    verify_fixtures,
    FIXTURE_100_SINGLE,
    FIXTURE_50_50_SPLIT,
    FIXTURE_90_10_CANARY,
    FIXTURE_EMPTY_TRAFFIC,
    FIXTURE_WITH_CANDIDATE,
)


def test_verify_fixtures_passes():
    assert verify_fixtures() is True


def test_100_percent_single_revision():
    alloc = parse_traffic_allocations(FIXTURE_100_SINGLE)
    assert alloc == "studio-tower-api-prod-001=100"
    primary = parse_primary_revision(FIXTURE_100_SINGLE)
    assert primary == "studio-tower-api-prod-001"

    cmd = build_rollback_command("studio-tower-api", "my-project", "us-central1", alloc, primary)
    assert cmd == [
        "gcloud",
        "run",
        "services",
        "update-traffic",
        "studio-tower-api",
        "--project=my-project",
        "--region=us-central1",
        "--to-revisions=studio-tower-api-prod-001=100",
    ]


def test_50_50_split_traffic():
    alloc = parse_traffic_allocations(FIXTURE_50_50_SPLIT)
    assert alloc == "studio-tower-api-blue=50,studio-tower-api-green=50"
    primary = parse_primary_revision(FIXTURE_50_50_SPLIT)
    assert primary == "studio-tower-api-blue"

    cmd = build_rollback_command("studio-tower-api", "tenant-proj", "us-central1", alloc, primary)
    assert cmd == [
        "gcloud",
        "run",
        "services",
        "update-traffic",
        "studio-tower-api",
        "--project=tenant-proj",
        "--region=us-central1",
        "--to-revisions=studio-tower-api-blue=50,studio-tower-api-green=50",
    ]


def test_90_10_canary_traffic():
    alloc = parse_traffic_allocations(FIXTURE_90_10_CANARY)
    assert alloc == "studio-tower-api-stable=90,studio-tower-api-canary=10"
    primary = parse_primary_revision(FIXTURE_90_10_CANARY)
    assert primary == "studio-tower-api-stable"

    cmd = build_rollback_command("studio-tower-api", "prod-project", "us-central1", alloc, primary)
    assert cmd == [
        "gcloud",
        "run",
        "services",
        "update-traffic",
        "studio-tower-api",
        "--project=prod-project",
        "--region=us-central1",
        "--to-revisions=studio-tower-api-stable=90,studio-tower-api-canary=10",
    ]


def test_empty_traffic_fallback():
    alloc = parse_traffic_allocations(FIXTURE_EMPTY_TRAFFIC)
    assert alloc == "studio-tower-api-initial=100"
    primary = parse_primary_revision(FIXTURE_EMPTY_TRAFFIC)
    assert primary == "studio-tower-api-initial"


def test_candidate_tag_url_parsing():
    url = parse_candidate_url(FIXTURE_WITH_CANDIDATE, "candidate-20260908")
    assert url == "https://candidate-20260908---studio-tower-api-prod-uc.a.run.app"


def test_service_url_parsing():
    url = parse_service_url(FIXTURE_100_SINGLE)
    assert url == "https://studio-tower-api-prod-uc.a.run.app"


def test_traffic_allocations_sum_must_be_100():
    bad_spec = {
        "status": {
            "traffic": [
                {"revisionName": "rev-1", "percent": 60},
                {"revisionName": "rev-2", "percent": 30},
            ]
        }
    }
    with pytest.raises(ValueError, match="do not sum to 100%"):
        parse_traffic_allocations(bad_spec)


def test_traffic_allocations_invalid_percent():
    bad_spec = {
        "status": {
            "traffic": [
                {"revisionName": "rev-1", "percent": -5},
            ]
        }
    }
    with pytest.raises(ValueError, match="Invalid traffic percent"):
        parse_traffic_allocations(bad_spec)


def test_candidate_url_missing_tag_fails_closed():
    with pytest.raises(ValueError, match="not found in Cloud Run traffic status"):
        parse_candidate_url(FIXTURE_WITH_CANDIDATE, "nonexistent-tag")


def test_candidate_url_non_https_fails_closed():
    bad_spec = {
        "status": {
            "traffic": [
                {"tag": "cand-insecure", "url": "http://candidate.insecure.run.app", "percent": 0},
            ]
        }
    }
    with pytest.raises(ValueError, match="not HTTPS"):
        parse_candidate_url(bad_spec, "cand-insecure")


def test_service_url_missing_fails_closed():
    with pytest.raises(ValueError, match="status.url"):
        parse_service_url({"status": {}})
