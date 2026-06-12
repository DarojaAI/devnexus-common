# Publish workflow setup — add this repo to the seedwork

Your publish workflow (`publish-vpc-runner-base.yml`) needs four GCP-side things before it'll work:

1. An Artifact Registry repo
2. A service account with write access
3. A Workload Identity Federation binding (so the workflow can assume the SA, no keys required)
4. Four repo variables that the workflow reads

Instead of hand-rolling this once per repo, **`DarojaAI/dev-nexus/terraform/seedwork/` does it automatically via a single `terraform apply`.**

## What you need to do

Add one block to `terraform/seedwork/seedwork.tfvars` in the dev-nexus repo:

```hcl
{
  name       = "devnexus-common"
  sa_id      = "github-publish-devnexus-common"
  ar_repo_id = "devnexus-common"
  # region: optional, defaults to us-central1
}
```

Push to main. The `seedwork-apply` workflow runs, creates the AR repo, the service account, the WIF binding, and sets these four variables on this repo:

- `GCP_PROJECT_ID`
- `GCP_REGION`
- `GCP_WIF_PROVIDER`
- `GCP_PUBLISH_SA`

No manual gcloud. No clicking around the GitHub UI. The next `v*` tag push will pick up the variables and publish the image end-to-end.

## If you need to do this by hand first (before the seedwork runs)

See `README-publish-vpc-runner-base.md` in this directory for the five sequential steps: AR repo, SA, WIF binding, repo variables, verify.

That document also explains why we use WIF instead of service account keys, and troubleshooting tips if something goes wrong.

## For more detail

- `DarojaAI/dev-nexus/terraform/seedwork/README.md` — the canonical design and tech reference for the seedwork root itself.
