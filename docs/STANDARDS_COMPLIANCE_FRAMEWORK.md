# Standards Compliance Framework

**Define and verify which standards apply to backend, frontend, and shared repositories**

This framework specifies:
- Which standards apply to each repository type
- Compliance requirements and enforcement levels
- Automated verification procedures
- How to check compliance across projects

---

## Standards Matrix

| Standard | Backend | Frontend | Shared | Notes |
|----------|---------|----------|--------|-------|
| **API Documentation Automation** | REQUIRED | N/A | N/A | Backend serves the API |
| **Frontend API Integration Guide** | RECOMMENDED | REQUIRED | N/A | Frontend must follow integration patterns |
| **Skill Metadata Decorators** | REQUIRED | N/A | N/A | Backend marks breaking changes |
| **TypeScript Types Generation** | REQUIRED | RECOMMENDED | N/A | Backend generates, frontend uses |
| **CHANGELOG.md** | REQUIRED | OPTIONAL | OPTIONAL | Document changes |
| **BREAKING_CHANGES.md** | REQUIRED | OPTIONAL | OPTIONAL | Reference for migrations |
| **A2A Manifest Endpoint** | REQUIRED | N/A | N/A | Runtime API registry |
| **Request/Response Validation** | RECOMMENDED | REQUIRED | N/A | Frontend validates before sending |
| **API Documentation** | REQUIRED | RECOMMENDED | N/A | Backend maintains truth |
| **Integration Tests** | RECOMMENDED | REQUIRED | N/A | Frontend tests against API |
| **Version Pinning** | OPTIONAL | RECOMMENDED | N/A | Frontend pins to known API version |

---

## Repository Types & Requirements

### Backend Repository (`dev-nexus`)

**Responsibilities**:
- Define A2A skills with input/output schemas
- Mark breaking changes with `@breaking_change()` decorators
- Generate and maintain API documentation
- Serve manifest endpoint at `/.well-known/a2a-manifest.json`
- Provide TypeScript type definitions
- Track version history in CHANGELOG.md

**Required Standards**:
1. ✅ API Documentation Automation System
2. ✅ Skill Metadata Decorators (@breaking_change, @deprecated)
3. ✅ Manifest Generation (a2a-skills.json)
4. ✅ TypeScript Types Generation (a2a-types.ts)
5. ✅ Changelog (CHANGELOG.md)
6. ✅ Breaking Changes Reference (BREAKING_CHANGES.md)
7. ✅ Manifest Endpoint (/.well-known/a2a-manifest.json)
8. ✅ API Documentation (COMPONENTS.md, API_DOCUMENTATION_AUTOMATION.md)

**Compliance Check**:
```bash
python scripts/verify_api_docs_setup.py
# Must return: [SUCCESS] All checks passed
```

**CI/CD Requirements**:
- Regenerate manifest on skill changes
- Validate generated files are valid JSON/TypeScript/Markdown
- Commit generated files to repo
- Tag versions for release

---

### Frontend Repository (`dev-nexus-frontend`)

**Responsibilities**:
- Fetch API manifest from backend
- Use auto-generated TypeScript types
- Validate requests match API schema before sending
- Read BREAKING_CHANGES.md before updating backend version
- Implement skills according to spec

**Required Standards**:
1. ✅ Read Backend Documentation
   - Frontend Integration Guide: `../dev-nexus/docs/FRONTEND_API_INTEGRATION.md`
   - Breaking Changes Reference: `../dev-nexus/docs/BREAKING_CHANGES.md`
   - API Manifest: `../dev-nexus/docs/a2a-skills.json`

2. ✅ Type-Safe Integration
   - Import types from backend: `../dev-nexus/docs/a2a-types.ts`
   - Use TypeScript strict mode
   - Validate requests against schema before sending

3. ✅ Request Validation
   - Validate input against skill schema before calling
   - Use ajv or similar for schema validation
   - Proper error handling for validation failures

4. ✅ Integration Tests
   - Test against running backend
   - Verify response format matches schema
   - Test breaking change migration paths

5. ✅ Version Pinning (Optional but Recommended)
   - Document target backend version
   - Pin in package.json or similar
   - Test against specific backend version

**Compliance Check**:
```bash
python scripts/verify_frontend_integration.py
# Checks:
#  - Can read backend manifest
#  - Uses TypeScript types from backend
#  - Validates requests before sending
#  - Tests pass against backend API
```

**CI/CD Requirements**:
- Run integration tests against backend
- Validate API calls match backend schema
- Check for outdated backend version

---

### Shared/Monorepo Configuration

For monorepos with both backend and frontend:

**Structure**:
```
monorepo/
├── backend/           (backend standards apply)
├── frontend/          (frontend standards apply)
├── docs/
│   └── STANDARDS.md   (this file)
└── compliance/
    ├── audit.py       (verification script)
    ├── config.yaml    (standards config)
    └── reports/       (compliance reports)
```

**Unified Compliance Check**:
```bash
python compliance/audit.py --all
# Checks both backend and frontend standards
# Generates combined compliance report
```

---

## Compliance Levels

### Level 1: REQUIRED ✅ (Must Have)
- Fail CI/CD if not present
- Blocks PRs to main branch
- Must be fixed before release
- Example: `@breaking_change` decorators on backend

### Level 2: RECOMMENDED 📋 (Should Have)
- Warning in CI/CD if not present
- Can merge to main, but flagged
- Should be addressed before release
- Example: Integration tests on frontend

### Level 3: OPTIONAL 💡 (Nice to Have)
- No CI/CD enforcement
- Good practice but not required
- Example: Version pinning

---

## Verification Procedures

### Backend Compliance Check

```bash
# Quick check (< 5 seconds)
python scripts/verify_api_docs_setup.py

# Detailed check with explanations
python scripts/verify_api_docs_setup.py --verbose

# Generate compliance report
python scripts/verify_api_docs_setup.py --report compliance.json
```

**Output**: Pass/Fail for each standard

### Frontend Compliance Check

```bash
# Check integration with backend
python scripts/verify_frontend_integration.py

# Specific checks:
# - Can fetch backend manifest
# - Uses correct types
# - Validates requests
# - Tests pass
```

### Cross-Repository Audit

```bash
# Check both backend and frontend
python compliance/audit.py \
  --backend ../dev-nexus \
  --frontend ../dev-nexus-frontend \
  --report report.html

# Generates HTML report showing:
# - Backend compliance (✅/❌/⚠️)
# - Frontend compliance (✅/❌/⚠️)
# - API version match
# - Breaking changes status
# - Recommendations
```

---

## Configuration File (`compliance/config.yaml`)

```yaml
# Standards Compliance Configuration

standards:
  api_documentation_automation:
    applies_to: [backend]
    level: required
    description: "Automated API documentation system"
    checks:
      - files:
          - a2a/skill_metadata.py
          - scripts/generate_skills_manifest.py
          - scripts/generate_changelog.py
      - content:
          - "@breaking_change decorator exists"
          - "manifest endpoint exists"

  frontend_api_integration:
    applies_to: [frontend]
    level: required
    description: "Frontend uses backend API correctly"
    checks:
      - imports:
          - "Uses backend a2a-types.ts"
      - validation:
          - "Validates requests before sending"
      - testing:
          - "Integration tests pass"

  changelog:
    applies_to: [backend, shared]
    level: required
    description: "Track changes in CHANGELOG.md"
    files:
      - docs/CHANGELOG.md
    pattern: "# Changelog"

  breaking_changes_reference:
    applies_to: [backend]
    level: required
    description: "BREAKING_CHANGES.md for migrations"
    files:
      - docs/BREAKING_CHANGES.md
    pattern: "Breaking Changes"

backends:
  dev-nexus:
    type: backend
    location: ../dev-nexus
    enforce_level: required
    ci_cd: github_actions

frontends:
  dev-nexus-frontend:
    type: frontend
    location: ../dev-nexus-frontend
    backend_version: "main"  # Which backend version to target
    enforce_level: required
    ci_cd: github_actions
```

---

## CI/CD Integration

### Backend (GitHub Actions)

```yaml
name: Compliance Check - Backend

on: [push, pull_request]

jobs:
  compliance:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v3
      - uses: actions/setup-python@v4
      - name: Verify Standards Compliance
        run: |
          python scripts/verify_api_docs_setup.py
          if [ $? -ne 0 ]; then
            echo "FAILED: API Documentation Automation not properly set up"
            exit 1
          fi

      - name: Generate Compliance Report
        if: always()
        run: |
          python scripts/verify_api_docs_setup.py --report compliance.json

      - name: Upload Report
        if: always()
        uses: actions/upload-artifact@v3
        with:
          name: compliance-report
          path: compliance.json
```

### Frontend (GitHub Actions)

```yaml
name: Compliance Check - Frontend

on: [push, pull_request]

jobs:
  compliance:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v3
      - uses: actions/setup-node@v3
      - uses: actions/setup-python@v4

      - name: Fetch Backend API Spec
        run: |
          mkdir -p src/api
          curl https://raw.githubusercontent.com/DarojaAI/dev-nexus/main/docs/a2a-skills.json \
            -o src/api/a2a-skills.json
          curl https://raw.githubusercontent.com/DarojaAI/dev-nexus/main/docs/a2a-types.ts \
            -o src/api/a2a-types.ts

      - name: Check API Integration
        run: python scripts/verify_frontend_integration.py

      - name: Run Integration Tests
        run: npm run test:integration -- --backend http://localhost:8080
```

### Cross-Repository Audit

```yaml
name: Compliance Audit - All Repos

on:
  schedule:
    - cron: '0 0 * * 0'  # Weekly
  workflow_dispatch:

jobs:
  audit:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v3
        with:
          path: main-repo

      - name: Checkout Backend
        uses: actions/checkout@v3
        with:
          repository: DarojaAI/dev-nexus
          path: backend-repo

      - name: Checkout Frontend
        uses: actions/checkout@v3
        with:
          repository: DarojaAI/dev-nexus-frontend
          path: frontend-repo

      - uses: actions/setup-python@v4

      - name: Run Cross-Repository Audit
        run: |
          cd main-repo
          python compliance/audit.py \
            --backend ../backend-repo \
            --frontend ../frontend-repo \
            --report compliance-report.html

      - name: Upload Report
        uses: actions/upload-artifact@v3
        with:
          name: compliance-audit-report
          path: compliance-report.html

      - name: Post Results
        run: |
          # Post results to Slack, Discord, or GitHub Discussion
          python compliance/post_results.py compliance-report.html
```

---

## Compliance Reporting

### Report Format

```json
{
  "timestamp": "2026-02-24T18:30:00Z",
  "repositories": {
    "dev-nexus": {
      "type": "backend",
      "overall_status": "PASS",
      "score": 31/31,
      "standards": {
        "api_documentation_automation": {
          "status": "PASS",
          "level": "required",
          "checks": [
            {
              "name": "Metadata system exists",
              "status": "PASS"
            },
            {
              "name": "Manifest generator exists",
              "status": "PASS"
            },
            {
              "name": "@breaking_change decorator",
              "status": "PASS"
            }
          ]
        },
        "changelog": {
          "status": "PASS",
          "level": "required"
        },
        "breaking_changes_reference": {
          "status": "PASS",
          "level": "required"
        }
      },
      "recommendations": [],
      "warnings": []
    },
    "dev-nexus-frontend": {
      "type": "frontend",
      "overall_status": "WARN",
      "score": 8/9,
      "standards": {
        "frontend_api_integration": {
          "status": "WARN",
          "level": "required",
          "checks": [
            {
              "name": "Uses backend types",
              "status": "PASS"
            },
            {
              "name": "Validates requests",
              "status": "FAIL",
              "message": "No request validation found in src/api/client.ts"
            },
            {
              "name": "Integration tests pass",
              "status": "PASS"
            }
          ]
        }
      },
      "recommendations": [
        "Add request validation using ajv library"
      ],
      "warnings": [
        "Backend version mismatch: frontend expects v1.1.0, backend is v1.2.0"
      ]
    }
  },
  "cross_repository": {
    "api_version_match": "WARN",
    "frontend_backend_compatibility": "WARN",
    "recommendations": [
      "Update frontend to latest backend API version"
    ]
  }
}
```

### HTML Report Example

```html
<!DOCTYPE html>
<html>
<head>
  <title>Standards Compliance Report</title>
  <style>
    .pass { color: green; }
    .fail { color: red; }
    .warn { color: orange; }
    .repo-card { border: 1px solid #ccc; padding: 20px; margin: 10px 0; }
  </style>
</head>
<body>
  <h1>Standards Compliance Report</h1>
  <p>Generated: 2026-02-24 18:30 UTC</p>

  <div class="repo-card">
    <h2>dev-nexus (Backend)</h2>
    <p class="pass">Overall Status: PASS (31/31)</p>
    <ul>
      <li class="pass">✓ API Documentation Automation</li>
      <li class="pass">✓ Changelog</li>
      <li class="pass">✓ Breaking Changes Reference</li>
    </ul>
  </div>

  <div class="repo-card">
    <h2>dev-nexus-frontend (Frontend)</h2>
    <p class="warn">Overall Status: WARN (8/9)</p>
    <ul>
      <li class="pass">✓ Uses Backend Types</li>
      <li class="fail">✗ Request Validation (No validation found in client.ts)</li>
      <li class="pass">✓ Integration Tests</li>
    </ul>
    <h3>Recommendations</h3>
    <ul>
      <li>Add request validation using ajv library</li>
    </ul>
    <h3>Warnings</h3>
    <ul>
      <li>Backend version mismatch: targeting v1.1.0, backend is v1.2.0</li>
    </ul>
  </div>
</body>
</html>
```

---

## Onboarding Checklist

When starting work on a new repository:

### For Backend Developers

- [ ] Read: `docs/STANDARDS_COMPLIANCE_FRAMEWORK.md`
- [ ] Read: `docs/API_DOCUMENTATION_SETUP_CHECKLIST.md`
- [ ] Run: `python scripts/verify_api_docs_setup.py`
- [ ] Understand: Decorator system in `a2a/skill_metadata.py`
- [ ] Check: Your skills have `@breaking_change()` on API changes
- [ ] Before PR: Regenerate manifest and changelog
- [ ] Check: CI/CD compliance passes

### For Frontend Developers

- [ ] Read: `docs/STANDARDS_COMPLIANCE_FRAMEWORK.md`
- [ ] Read: `../backend/docs/FRONTEND_API_INTEGRATION.md`
- [ ] Understand: Backend API spec in `../backend/docs/a2a-skills.json`
- [ ] Use: TypeScript types from `../backend/docs/a2a-types.ts`
- [ ] Implement: Request validation before API calls
- [ ] Write: Integration tests for API calls
- [ ] Check: CI/CD compliance passes

---

## Enforcement Strategy

### Pull Request (PR) Requirements

**Backend PRs**:
- ✅ Compliance check passes (automated)
- ✅ Tests pass
- ✅ Manifest and changelog regenerated
- ✅ Breaking changes documented

**Frontend PRs**:
- ✅ Integration tests pass (automated)
- ✅ Uses backend types correctly
- ✅ Request validation works
- ✅ Handles API errors

**Both**:
- ✅ No failing compliance checks
- ✅ At least 1 approval

### Release Requirements

**Backend Release**:
1. All compliance checks pass
2. CHANGELOG.md updated with version tag
3. Version number bumped
4. Tag created: `v1.2.0`
5. Release notes include breaking changes

**Frontend Release**:
1. All compliance checks pass
2. Backend API version pinned
3. Integration tests pass
4. README documents backend compatibility

---

## Escalation Process

If compliance fails:

### Level 1: Automated Feedback
- CI/CD fails with clear message
- Link to remediation guide
- Example: "Request validation missing - see FRONTEND_API_INTEGRATION.md"

### Level 2: Review Request
- If PR fails compliance, request reviewer
- Reviewer checks if exemption is warranted
- Link to standards framework

### Level 3: Exception Tracking
- If exemption granted, create issue
- Track technical debt
- Plan remediation

---

## See Also

- API_DOCUMENTATION_SETUP_CHECKLIST.md - Backend setup guide
- FRONTEND_API_INTEGRATION.md - Frontend integration guide
- API_DOCUMENTATION_AUTOMATION.md - System overview
- CLAUDE.md - Backend developer guide
