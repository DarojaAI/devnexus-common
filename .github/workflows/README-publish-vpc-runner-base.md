# Publish VPC Runner Base Image — setup guide

This document explains the one-time GCP + repo configuration needed before
`.github/workflows/publish-vpc-runner-base.yml` will run successfully.

For the *design* behind this bootstrap (why the trust boundary lives in
dev-nexus's seedwork, what the second WIF pool is for, how the four
repo variables get populated), see
[`PUBLISH_BOOTSTRAP.md`](./PUBLISH_BOOTSTRAP.md) in this directory.
This file is the operational "do it by hand" guide, kept for cases
where the seedwork hasn't been applied for a repo yet.

The workflow is triggered on every `v*` tag push. After a `feat:` commit
merges to `main`, semantic-release cuts a tag (e.g. `v1.9.0`) and this
workflow publishes a corresponding base image to Artifact Registry:

```
${REGION}-docker.pkg.dev/${PROJECT_ID}/devnexus-common/vpc-runner-base:v1.9.0
${REGION}-docker.pkg.dev/${PROJECT_ID}/devnexus-common/vpc-runner-base:latest
```

Consumer repos (dev-nexus, rag-research-tool) then `FROM` the version-pinned
URL instead of rebuilding the toolchain on every CI run.

## TL;DR — for repos that are already in the seedwork list

If `devnexus-common` is already an entry in
`DarojaAI/dev-nexus/terraform/seedwork/seedwork.tfvars`, this whole
section is handled for you. The four repo variables below
(`GCP_PROJECT_ID`, `GCP_REGION`, `GCP_WIF_PROVIDER`, `GCP_PUBLISH_SA`)
are set automatically by the seedwork-apply workflow. The AR repo, the
publishing SA, the WIF binding, and the writer IAM policy are all
created by the same apply. **You only need this README's manual steps
if you're adding a brand-new publishing repo from scratch, before
the seedwork has run for it.**

## One-time setup

### 1. Artifact Registry: create the repo (if it doesn't exist)

```bash
gcloud artifacts repositories create devnexus-common \
  --repository-format=docker \
  --location=us-central1 \
  --description="Shared base images for DarojaAI projects (vpc-runner, etc.)"
```

Adjust `--location` if your default AR region is different. The
`--location` value must match the `GCP_REGION` variable you set below.

### 2. Service account: create the publisher

```bash
PROJECT_ID="<your-gcp-project>"
SA_NAME="github-publish-devnexus-common"
SA_EMAIL="${SA_NAME}@${PROJECT_ID}.iam.gserviceaccount.com"

gcloud iam service-accounts create "$SA_NAME" \
  --project="$PROJECT_ID" \
  --display-name="Publish devnexus-common base images from CI"

# Grant write access to the AR repo
gcloud projects add-iam-policy-binding "$PROJECT_ID" \
  --member="serviceAccount:${SA_EMAIL}" \
  --role="roles/artifactregistry.writer"
```

This SA has the *narrowest* scope the workflow needs: write access to
Artifact Registry. It has no project-level admin roles, no service account
key material, and no compute/storage permissions. The WIF binding below
constrains *when* the SA can be used.

### 3. Workload Identity Federation: bind the SA to this repo

Use Terraform, `gcloud iam`, or the GitHub OIDC provider of your choice.
The pattern in this org is the `DarojaAI/infra-actions/gcp/auth` action,
which expects:

- A WIF provider resource name (`projects/.../locations/.../workloadIdentityPools/.../providers/...`)
- A pool that trusts `token.actions.githubusercontent.com`
- An attribute condition that limits which repo + branch/ref can mint tokens
- An IAM binding granting `roles/iam.workloadIdentityUser` on the SA to the
  pool's principalSet

The condition should look something like:

```
attribute.repository == "DarojaAI/devnexus-common"
```

If you also want `workflow_dispatch` runs to be able to publish (e.g. for a
manual `dev` tag), the condition can be relaxed to allow any ref; or you
can add explicit `main` and `v*` allowances. Keep it tight for prod.

### 4. Repo variables: set on DarojaAI/devnexus-common

Go to **Settings → Secrets and variables → Actions → Variables → New
repository variable** and add:

| Name | Required | Example | Notes |
|---|---|---|---|
| `GCP_PROJECT_ID` | yes | `globalbiting-dev` | Project that hosts the AR repo. |
| `GCP_REGION` | no | `us-central1` | Defaults to `us-central1` if unset. |
| `GCP_WIF_PROVIDER` | yes | `projects/12345/locations/global/workloadIdentityPools/github-pool/providers/github-provider` | WIF provider resource name. |
| `GCP_PUBLISH_SA` | yes | `github-publish-devnexus-common@globalbiting-dev.iam.gserviceaccount.com` | SA email created in step 2. |

No secrets are required — WIF handles auth.

### 5. Verify

The cleanest verification is to cut a throwaway tag and confirm the image
lands:

```bash
git tag v0.0.0-test-publish
git push origin v0.0.0-test-publish
# Watch the Actions tab. After it succeeds:
gcloud container images list-tags \
  us-central1-docker.pkg.dev/globalbiting-dev/devnexus-common/vpc-runner-base
# Then delete the test tag to keep the registry clean:
git tag -d v0.0.0-test-publish
git push origin :v0.0.0-test-publish
# And delete the matching image tags in AR:
gcloud container images delete \
  us-central1-docker.pkg.dev/globalbiting-dev/devnexus-common/vpc-runner-base:v0.0.0-test-publish \
  --quiet --force-delete-tags
```

The actual `v1.9.0` publish will happen automatically when PR #42 merges
and semantic-release cuts the tag.

## Why WIF, not a service account key

WIF (Workload Identity Federation) lets the workflow assume the SA via
short-lived OIDC tokens minted by GitHub Actions. There is no
long-lived service account key to leak. This is the modern pattern and
matches what the rest of the DarojaAI org already does (see
`DarojaAI/dev-nexus/.github/workflows/terraform-plan.yml` for an example).

## Troubleshooting

- **`Missing required repo variables`** — the `Validate required
  configuration` step prints the missing variable names. Go set them.
- **`Permission denied` on `docker push`** — the SA doesn't have
  `roles/artifactregistry.writer` on the AR repo. Re-run step 2.
- **`Invalid authentication credentials`** — the WIF pool doesn't trust
  this repo, or the attribute condition is too tight. Re-check step 3.
- **Tag exists in AR but the workflow says `image not found`** — old
  cached `gcloud` config. Add a `gcloud auth login --update-adc` step.
