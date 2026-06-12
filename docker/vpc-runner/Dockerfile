# =============================================================================
# VPC Runner — canonical Cloud Run Job image for DarojaAI projects
# =============================================================================
# This is the canonical source of truth for the "vpc-runner" / "terraform-vpc"
# Cloud Run Job image used across DarojaAI projects. It lives in
# `devnexus-common` so all consumers (dev-nexus, rag-research-tool, future
# projects) build the same toolchain.
#
# What it provides:
#   - gcloud CLI (pinned to a specific version, not :latest) for `gcloud run
#     jobs execute` and any other GCP operations the job needs
#   - Terraform (pinned TERRAFORM_VERSION) for terraform init/plan/apply
#   - Atlas CLI (pinned ATLAS_VERSION) with the community-edition driver set
#     so `atlas migrate apply` works against the project DB
#   - psql, jq, git, bash, ca-certificates as runtime essentials
#
# It does NOT copy project files (atlas migrations, terraform config, scripts).
# Consumers must extend this image and add their own COPYs. See README.md for
# three consumption patterns.
#
# Versioning:
#   - Base image (gcloud CLI) is pinned by version tag, not :latest.
#   - TERRAFORM_VERSION and ATLAS_VERSION are ARG defaults — consumers can
#     override at build time with `--build-arg TERRAFORM_VERSION=1.15.1`.
#   - When bumping TERRAFORM_VERSION or ATLAS_VERSION, ensure both consumers
#     that share a Terraform state file (e.g. dev-nexus + rag-research-tool)
#     bump in lockstep. State-file format is not stable across major
#     Terraform versions, and a v0.x → v1.x Atlas upgrade can change schema
#     diff output.
# =============================================================================

# -----------------------------------------------------------------------------
# Stage 1: builder — install Terraform and Atlas CLI
# -----------------------------------------------------------------------------
FROM golang:1.22-alpine AS builder

ARG TERRAFORM_VERSION=1.15.1
ARG ATLAS_VERSION=v1.2.0

# Install Terraform (HashiCorp official release, no checksum verification
# in this base — consumers who care about supply-chain integrity should add
# a `.dockerignore`-able verify step or a SLSA provenance check).
RUN apk add --no-cache curl unzip bash && \
    curl -fsSL "https://releases.hashicorp.com/terraform/${TERRAFORM_VERSION}/terraform_${TERRAFORM_VERSION}_linux_amd64.zip" -o terraform.zip && \
    unzip terraform.zip -d /usr/local/bin && \
    rm terraform.zip && \
    terraform version

# Install Atlas CLI with all database drivers (via official installer).
# The raw release.ariga.io binary lacks bundled drivers; atlasgo.sh installs
# the full community edition with postgres support. Pinned to ATLAS_VERSION
# so all consumers run the same Atlas version against the same migrations.
# The --community flag is explicit so a future "extended" release does not
# silently swap in a different driver set.
RUN curl -sSf https://atlasgo.sh | sh -s -- -y --community ${ATLAS_VERSION} && \
    atlas version

# -----------------------------------------------------------------------------
# Stage 2: runtime — slim base with gcloud CLI and runtime tools
# -----------------------------------------------------------------------------
FROM gcr.io/google.com/cloudsdktool/google-cloud-cli:572.0.0-alpine

WORKDIR /workspace

# Runtime essentials only. Build-time tools (curl, unzip) live in the
# builder stage and don't bloat the runtime image.
#   - postgresql-client: psql for direct DB queries
#   - jq: env-var parsing in workflows
#   - git: required by Terraform for some providers
#   - bash: workflows expect /bin/bash, alpine's busybox sh works but bash
#     is more predictable for `set -euo pipefail` style scripts
#   - ca-certificates: required for HTTPS calls (kept explicit even though
#     gcloud's base image likely has it; no harm being explicit)
RUN apk add --no-cache \
    postgresql-client \
    jq \
    git \
    bash \
    ca-certificates \
    && rm -rf /var/cache/apk/*

# Copy Terraform and Atlas from the builder stage
COPY --from=builder /usr/local/bin/terraform /usr/local/bin/terraform
COPY --from=builder /usr/local/bin/atlas     /usr/local/bin/atlas

# Default entrypoint: `/bin/sh -c "<args>"`. Consumers can override by
# appending their own ENTRYPOINT in a wrapper Dockerfile. The default CMD
# is a no-op so the image can also be invoked as
# `docker run <image>` to verify the toolchain.
ENTRYPOINT ["/bin/sh", "-c"]
CMD ["echo 'VPC Runner ready. Use gcloud run jobs execute with --args'"]
