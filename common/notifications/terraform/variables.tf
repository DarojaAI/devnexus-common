# ====================================
# Generic GCP → Discord Notifier Variables
# ====================================

variable "project_id" {
  description = "GCP project ID"
  type        = string
}

variable "region" {
  description = "GCP region"
  type        = string
  default     = "us-central1"
}

variable "app_name" {
  description = "Name prefix for all resources (Pub/Sub topic, Cloud Run service, service account)"
  type        = string
}

variable "discord_webhook_url" {
  description = "Discord channel webhook URL for notifications"
  type        = string
  sensitive   = true
}

variable "deploy_notifier" {
  description = <<-EOT
    When true: deploy the Cloud Run notifier service in this project.
    When false: only create the Pub/Sub topic + subscription pointing at
    an existing notifier URL (set existing_notifier_url).
    Use one project to deploy the shared notifier; all others reuse it.
  EOT
  type        = bool
  default     = false
}

variable "existing_notifier_url" {
  description = "URL of an already-deployed notifier (when deploy_notifier = false)"
  type        = string
  default     = ""
}

variable "allow_unauthenticated" {
  description = "Allow public access to the notifier push endpoint"
  type        = bool
  default     = true
}

variable "notification_trigger_enabled" {
  description = "Create a Cloud Build GitHub push trigger that publishes build events"
  type        = bool
  default     = false
}

variable "github_owner" {
  description = "GitHub repository owner (for the trigger)"
  type        = string
  default     = ""
}

variable "github_repo" {
  description = "GitHub repository name (for the trigger)"
  type        = string
  default     = ""
}

variable "github_branch" {
  description = "GitHub branch to trigger on"
  type        = string
  default     = "main"
}
