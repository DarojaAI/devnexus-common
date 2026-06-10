#!/usr/bin/env python3
"""
Standards Compliance Audit Tool

Verifies compliance with standards across multiple repositories (backend, frontend, etc).
Generates comprehensive compliance reports.

Usage:
    python compliance/audit.py                           # Audit current repo
    python compliance/audit.py --config standards-config.yaml
    python compliance/audit.py --all --report report.json
    python compliance/audit.py --watch                   # Watch for changes
"""

import sys
import json
import yaml
import argparse
from pathlib import Path
from typing import Dict, Any
from datetime import datetime
import re


class ComplianceAudit:
    """Audit repository compliance with standards"""

    def __init__(self, config_path: str = "compliance/standards-config.yaml"):
        self.config_path = Path(config_path)
        self.config = self._load_config()
        self.results: Dict[str, Any] = {
            "timestamp": datetime.utcnow().isoformat() + "Z",
            "repositories": {},
            "cross_repository": {},
        }

    def _load_config(self) -> Dict:
        """Load standards configuration"""
        if not self.config_path.exists():
            print(f"[WARN] Config file not found: {self.config_path}")
            return {}

        try:
            with open(self.config_path, "r") as f:
                return yaml.safe_load(f) or {}
        except Exception as e:
            print(f"[ERROR] Failed to load config: {e}")
            return {}

    def audit_repository(self, repo_path: str, repo_type: str) -> Dict[str, Any]:
        """Audit a single repository"""
        repo_root = Path(repo_path)

        if not repo_root.exists():
            return {
                "status": "ERROR",
                "error": f"Repository not found: {repo_path}",
                "standards": {},
            }

        standards_to_check = self._get_standards_for_type(repo_type)
        standards_results = {}

        for standard_id, standard in standards_to_check.items():
            result = self._check_standard(repo_root, standard)
            standards_results[standard_id] = result

        # Calculate overall status
        passed = sum(1 for s in standards_results.values() if s["status"] == "PASS")
        failed = sum(1 for s in standards_results.values() if s["status"] == "FAIL")
        warned = sum(1 for s in standards_results.values() if s["status"] == "WARN")

        overall_status = "PASS" if failed == 0 else "FAIL"
        if warned > 0 and failed == 0:
            overall_status = "WARN"

        return {
            "type": repo_type,
            "status": overall_status,
            "score": f"{passed}/{passed + failed}",
            "passed": passed,
            "failed": failed,
            "warned": warned,
            "standards": standards_results,
        }

    def _get_standards_for_type(self, repo_type: str) -> Dict[str, Any]:
        """Get standards that apply to a repository type"""
        standards = {}

        for standard_id, standard in self.config.get("standards", {}).items():
            if repo_type in standard.get("applies_to", []):
                standards[standard_id] = standard

        return standards

    def _check_standard(
        self, repo_root: Path, standard: Dict[str, Any]
    ) -> Dict[str, Any]:
        """Check if a standard is met"""
        standard_id = standard.get("id", "unknown")
        level = standard.get("level", "optional")

        # Check required files
        file_checks = []
        for file_config in standard.get("files", []):
            path_pattern = file_config.get("path", "")
            required = file_config.get("required", False)
            description = file_config.get("description", "")

            if "*" in path_pattern:
                # Glob pattern
                matching_files = list(repo_root.glob(path_pattern))
                exists = len(matching_files) > 0
            else:
                # Specific file
                exists = (repo_root / path_pattern).exists()

            file_checks.append(
                {
                    "path": path_pattern,
                    "exists": exists,
                    "required": required,
                    "description": description,
                }
            )

        # Check content
        content_checks = []
        for content_config in standard.get("content_checks", []):
            file_path = content_config.get("file", "")
            pattern = content_config.get("pattern", "")
            description = content_config.get("description", "")

            file_full_path = repo_root / file_path

            if not file_full_path.exists():
                content_checks.append(
                    {
                        "file": file_path,
                        "pattern": pattern,
                        "found": False,
                        "description": description,
                        "reason": "File not found",
                    }
                )
                continue

            try:
                content = file_full_path.read_text(encoding="utf-8", errors="ignore")
            except UnicodeDecodeError:
                content = file_full_path.read_text(encoding="latin-1", errors="ignore")

            found = re.search(pattern, content) is not None

            content_checks.append(
                {
                    "file": file_path,
                    "pattern": pattern,
                    "found": found,
                    "description": description,
                }
            )

        # Determine status
        file_failures = [c for c in file_checks if not c["exists"] and c["required"]]
        content_failures = [c for c in content_checks if not c["found"]]

        if file_failures or content_failures:
            status = "FAIL"
        else:
            status = "PASS"

        return {
            "id": standard_id,
            "status": status,
            "level": level,
            "title": standard.get("title", ""),
            "description": standard.get("description", ""),
            "file_checks": file_checks,
            "content_checks": content_checks,
            "failures": file_failures + content_failures,
        }

    def audit_backend(self, repo_path: str) -> Dict[str, Any]:
        """Audit backend repository"""
        return self._audit_repo(repo_path, "backend")

    def audit_frontend(self, repo_path: str) -> Dict[str, Any]:
        """Audit frontend repository"""
        return self._audit_repo(repo_path, "frontend")

    def _audit_repo(self, repo_path: str, repo_type: str) -> Dict[str, Any]:
        """Audit a repository"""
        repo_name = Path(repo_path).name
        result = self.audit_repository(repo_path, repo_type)
        self.results["repositories"][repo_name] = result
        return result

    def generate_text_report(self) -> str:
        """Generate text format report"""
        lines = [
            "=" * 70,
            "STANDARDS COMPLIANCE AUDIT REPORT",
            "=" * 70,
            f"Generated: {self.results['timestamp']}",
            "",
        ]

        for repo_name, repo_result in self.results.get("repositories", {}).items():
            lines.append(f"\n{repo_name} ({repo_result.get('type', 'unknown')})")
            lines.append("-" * 70)

            status = repo_result.get("status", "UNKNOWN")
            score = repo_result.get("score", "0/0")
            lines.append(f"Status: {status} | Score: {score}")
            lines.append("")

            for standard_id, standard_result in repo_result.get(
                "standards", {}
            ).items():
                status = standard_result.get("status", "UNKNOWN")
                title = standard_result.get("title", standard_id)
                level = standard_result.get("level", "")

                icon = "✓" if status == "PASS" else "✗" if status == "FAIL" else "⚠"
                lines.append(f"  [{icon}] {title} ({level})")

                failures = standard_result.get("failures", [])
                if failures:
                    for failure in failures[:3]:  # Show first 3 failures
                        reason = failure.get("reason", failure.get("path", "Unknown"))
                        lines.append(f"      - {reason}")

        return "\n".join(lines)

    def generate_json_report(self) -> str:
        """Generate JSON format report"""
        return json.dumps(self.results, indent=2)

    def generate_html_report(self, output_path: str = "compliance-report.html"):
        """Generate HTML format report"""
        html = """<!DOCTYPE html>
<html>
<head>
    <title>Standards Compliance Report</title>
    <style>
        body { font-family: Arial, sans-serif; margin: 20px; }
        .header { background: #f0f0f0; padding: 20px; border-radius: 5px; }
        .timestamp { color: #666; font-size: 0.9em; }
        .repo-card { border: 1px solid #ddd; padding: 20px; margin: 20px 0; border-radius: 5px; }
        .pass { color: #28a745; }
        .fail { color: #dc3545; }
        .warn { color: #ffc107; }
        .standard-row { margin: 10px 0; padding: 10px; background: #f9f9f9; border-left: 4px solid #ddd; }
        .standard-row.pass { border-left-color: #28a745; }
        .standard-row.fail { border-left-color: #dc3545; }
        .standard-row.warn { border-left-color: #ffc107; }
        .failures { margin-left: 20px; color: #666; font-size: 0.9em; }
        .score { font-size: 1.2em; font-weight: bold; margin: 10px 0; }
    </style>
</head>
<body>
    <div class="header">
        <h1>Standards Compliance Report</h1>
        <p class="timestamp">Generated: {timestamp}</p>
    </div>
"""

        for repo_name, repo_result in self.results.get("repositories", {}).items():
            status_class = repo_result.get("status", "").lower()
            score = repo_result.get("score", "0/0")
            repo_type = repo_result.get("type", "unknown")

            html += f"""
    <div class="repo-card">
        <h2>{repo_name}</h2>
        <p><strong>Type:</strong> {repo_type}</p>
        <p class="score {status_class}">Status: {status_class.upper()} | Score: {score}</p>
        <ul>
"""

            for standard_id, standard_result in repo_result.get(
                "standards", {}
            ).items():
                status = standard_result.get("status", "").lower()
                title = standard_result.get("title", standard_id)
                level = standard_result.get("level", "")
                failures = standard_result.get("failures", [])

                html += f"""
            <li>
                <div class="standard-row {status}">
                    <strong>{title}</strong> ({level})
                    <div>Status: {status.upper()}</div>
"""

                if failures:
                    html += '                    <div class="failures"><strong>Issues:</strong><ul>'
                    for failure in failures[:3]:
                        reason = failure.get("reason", failure.get("path", "Unknown"))
                        html += f"<li>{reason}</li>"
                    html += "</ul></div>"

                html += """
                </div>
            </li>
"""

            html += """
        </ul>
    </div>
"""

        html += """
</body>
</html>
"""

        html = html.format(timestamp=self.results["timestamp"])

        with open(output_path, "w") as f:
            f.write(html)

        return output_path


def main():
    parser = argparse.ArgumentParser(
        description="Audit standards compliance across repositories"
    )
    parser.add_argument("--backend", "-b", help="Backend repository path")
    parser.add_argument("--frontend", "-f", help="Frontend repository path")
    parser.add_argument(
        "--all", "-a", action="store_true", help="Audit all configured repos"
    )
    parser.add_argument(
        "--config",
        "-c",
        default="compliance/standards-config.yaml",
        help="Configuration file",
    )
    parser.add_argument(
        "--format",
        choices=["text", "json", "html"],
        default="text",
        help="Report format",
    )
    parser.add_argument("--report", "-r", help="Report output file (for json/html)")
    parser.add_argument("--watch", "-w", action="store_true", help="Watch for changes")

    args = parser.parse_args()

    audit = ComplianceAudit(args.config)

    # Audit repositories
    if args.backend:
        audit.audit_backend(args.backend)
    if args.frontend:
        audit.audit_frontend(args.frontend)
    if args.all:
        # Audit all configured repos
        for repo_config in audit.config.get("repositories", []):
            repo_type = repo_config.get("type", "unknown")
            print(f"Auditing {repo_config['name']} ({repo_type})...")

    # Generate report
    if args.format == "json":
        report = audit.generate_json_report()
        if args.report:
            with open(args.report, "w") as f:
                f.write(report)
            print(f"[OK] Report saved to {args.report}")
        else:
            print(report)

    elif args.format == "html":
        report_path = args.report or "compliance-report.html"
        audit.generate_html_report(report_path)
        print(f"[OK] Report saved to {report_path}")

    else:  # text
        report = audit.generate_text_report()
        if args.report:
            with open(args.report, "w") as f:
                f.write(report)
            print(f"[OK] Report saved to {args.report}")
        else:
            print(report)

    return 0


if __name__ == "__main__":
    sys.exit(main())
