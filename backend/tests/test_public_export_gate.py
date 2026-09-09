from pathlib import Path
import importlib.util
import sys

ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))
from list_public_export import iter_public_export_paths  # noqa: E402

_SPEC = importlib.util.spec_from_file_location("export_public_sync", ROOT / "scripts" / "export_public_sync.py")
assert _SPEC and _SPEC.loader
_EXPORT = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(_EXPORT)
scan_staging_for_secrets = _EXPORT.scan_staging_for_secrets


def test_scan_staging_for_secrets_flags_live_tokens(tmp_path: Path):
    staging = tmp_path / "staging"
    staging.mkdir()
    (staging / "ok.md").write_text("GRAFANA_OTLP_TOKEN=glc_...\nGEMINI_API_KEY=AIzaSy...\n", encoding="utf-8")
    (staging / "leaked.env").write_text(
        "GRAFANA_OTLP_TOKEN=glc_eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9xxxx\n",
        encoding="utf-8",
    )
    hits = scan_staging_for_secrets(staging, ["ok.md", "leaked.env"])
    assert hits == ["leaked.env: grafana cloud write token"]


def test_scan_staging_allows_docs_placeholders_and_test_tokens(tmp_path: Path):
    staging = tmp_path / "staging"
    staging.mkdir()
    (staging / "INSTALLATION.md").write_text(
        "GRAFANA_SERVICE_ACCOUNT_TOKEN=glsa_...\nGRAFANA_OTLP_TOKEN=glc_...\nVITE_FIREBASE_API_KEY=AIzaSy...\n",
        encoding="utf-8",
    )
    app_dir = staging / "backend" / "app"
    app_dir.mkdir(parents=True)
    (app_dir / "config.py").write_text(
        'TOKEN = "glsa_test_visual_token"\nWRITE = "glc_test_token"\n',
        encoding="utf-8",
    )
    hits = scan_staging_for_secrets(staging, ["INSTALLATION.md", "backend/app/config.py"])
    assert hits == []


def test_scan_staging_flags_google_api_key_and_pem(tmp_path: Path):
    staging = tmp_path / "staging"
    staging.mkdir()
    (staging / "firebase.ts").write_text('apiKey: "AIzaSy0123456789abcdefghij"\n', encoding="utf-8")
    (staging / "id_rsa").write_text("-----BEGIN PRIVATE KEY-----\nMIIB\n", encoding="utf-8")
    hits = scan_staging_for_secrets(staging, ["firebase.ts", "id_rsa"])
    assert "firebase.ts: google api key" in hits
    assert "id_rsa: private key pem" in hits


def test_private_docs_are_not_in_public_export():
    names = {Path(rel).name for rel in iter_public_export_paths(ROOT)}
    assert "AI_DEVELOPMENT_LOG.md" not in names
    assert "FRONTEND_PRODUCT_IMPROVEMENT_PLAN.md" not in names
    assert "PROJECT_STORY.md" not in names
    assert "JUDGES_GUIDE.md" not in names
    assert "AGENT_SYNC.md" not in names
    assert "phase0_baseline.md" not in names
    assert "README.md" in names
    assert "PRIOR_ART.md" in names
