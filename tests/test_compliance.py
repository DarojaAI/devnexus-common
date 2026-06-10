"""Tests for the shared compliance module."""

from common.compliance import (
    get_readiness_checks,
    get_readiness_stages,
    load_standards_config,
)
from common.compliance.audit import ComplianceAudit


# ─── Config loading ─────────────────────────────────────────────────────────


def test_load_standards_config_returns_dict():
    """The bundled config is loaded successfully when the path resolves."""
    config = load_standards_config()
    assert isinstance(config, dict)
    assert "readiness_stages" in config or "standards" in config or config == {}


def test_load_standards_config_respects_explicit_path(tmp_path):
    """Explicit config_path argument bypasses the discovery search."""
    custom_config = tmp_path / "custom.yaml"
    custom_config.write_text("custom_key: value\nreadiness_stages: []\n")
    config = load_standards_config(str(custom_config))
    assert config["custom_key"] == "value"
    assert config["readiness_stages"] == []


def test_load_standards_config_returns_empty_dict_on_missing_file(
    monkeypatch, tmp_path, capsys
):
    """A missing config returns an empty dict, not an exception."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("STANDARDS_CONFIG_PATH", raising=False)
    config = load_standards_config("/nonexistent/path/to/config.yaml")
    assert config == {}
    captured = capsys.readouterr()
    assert "WARN" in captured.out or "ERROR" in captured.out


def test_load_standards_config_respects_env_var(tmp_path, monkeypatch):
    """STANDARDS_CONFIG_PATH env var wins over default discovery."""
    custom_config = tmp_path / "env-override.yaml"
    custom_config.write_text("source: env_var\n")
    monkeypatch.setenv("STANDARDS_CONFIG_PATH", str(custom_config))
    config = load_standards_config()
    assert config["source"] == "env_var"


def test_get_readiness_stages_returns_list():
    """Top-level helper returns the stages list (or empty list)."""
    stages = get_readiness_stages()
    assert isinstance(stages, list)


def test_get_readiness_checks_returns_dict():
    """Top-level helper returns the checks dict (or empty dict)."""
    checks = get_readiness_checks()
    assert isinstance(checks, dict)


# ─── Audit class ────────────────────────────────────────────────────────────


def test_audit_init_loads_config(tmp_path):
    """ComplianceAudit reads the standards config at init."""
    config_path = tmp_path / "standards-config.yaml"
    config_path.write_text("readiness_stages: []\nreadiness_checks: {}\n")
    audit = ComplianceAudit(str(config_path))
    assert audit.config == {"readiness_stages": [], "readiness_checks": {}}


def test_audit_init_returns_empty_on_missing_config(tmp_path, monkeypatch, capsys):
    """Audit doesn't crash when the config file is missing."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("STANDARDS_CONFIG_PATH", raising=False)
    audit = ComplianceAudit()
    assert audit.config == {}
    captured = capsys.readouterr()
    assert "WARN" in captured.out or "ERROR" in captured.out


def test_audit_returns_error_for_nonexistent_repo(tmp_path):
    """Auditing a missing repo path returns an error result."""
    config_path = tmp_path / "config.yaml"
    config_path.write_text("standards: {}\n")
    audit = ComplianceAudit(str(config_path))
    result = audit.audit_repository(str(tmp_path / "does-not-exist"), "backend")
    assert result["status"] == "ERROR"
    assert "not found" in result["error"].lower()


def test_audit_passes_when_required_files_present(tmp_path):
    """A repo with all required files gets PASS for the standard."""
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "CLAUDE.md").write_text("# Test\n")
    (repo / ".gitignore").write_text("node_modules\n.terraform\n")

    config_path = tmp_path / "config.yaml"
    config_path.write_text("""
standards:
  has_claude_and_gitignore:
    id: has_claude_and_gitignore
    title: "Has CLAUDE.md and .gitignore"
    description: "Test standard"
    applies_to: ["backend"]
    level: "required"
    files:
      - path: "CLAUDE.md"
        required: true
      - path: ".gitignore"
        required: true
    content_checks: []
""")
    audit = ComplianceAudit(str(config_path))
    audit.audit_backend(str(repo))
    result = audit.results["repositories"]["repo"]
    assert result["status"] == "PASS"
    assert result["passed"] == 1
    assert result["failed"] == 0


def test_audit_fails_when_required_files_missing(tmp_path):
    """A repo missing required files gets FAIL for the standard."""
    repo = tmp_path / "repo"
    repo.mkdir()

    config_path = tmp_path / "config.yaml"
    config_path.write_text("""
standards:
  has_claude_md:
    id: has_claude_md
    title: "Has CLAUDE.md"
    description: "Test standard"
    applies_to: ["backend"]
    level: "required"
    files:
      - path: "CLAUDE.md"
        required: true
    content_checks: []
""")
    audit = ComplianceAudit(str(config_path))
    audit.audit_backend(str(repo))
    result = audit.results["repositories"]["repo"]
    assert result["status"] == "FAIL"
    assert result["failed"] == 1
    assert result["passed"] == 0


def test_audit_text_report_includes_repo_name(tmp_path):
    """Text report includes the repo name in the output."""
    config_path = tmp_path / "config.yaml"
    config_path.write_text("standards: {}\n")
    repo = tmp_path / "my-repo"
    repo.mkdir()
    audit = ComplianceAudit(str(config_path))
    audit.audit_backend(str(repo))
    report = audit.generate_text_report()
    assert "my-repo" in report
    assert "STANDARDS COMPLIANCE" in report


def test_audit_json_report_is_valid_json(tmp_path):
    """JSON report round-trips through json.loads."""
    import json

    config_path = tmp_path / "config.yaml"
    config_path.write_text("standards: {}\n")
    repo = tmp_path / "my-repo"
    repo.mkdir()
    audit = ComplianceAudit(str(config_path))
    audit.audit_backend(str(repo))
    report = audit.generate_json_report()
    parsed = json.loads(report)
    assert "repositories" in parsed
    assert "timestamp" in parsed
