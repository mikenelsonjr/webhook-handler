# Every variable is typed and described. `project_id` and `region` have NO
# default on purpose: a default here silently targets the wrong project, which
# is the one Terraform mistake that is expensive rather than annoying. Being
# forced to say where you are deploying is the point.

variable "project_id" {
  type        = string
  description = "GCP project id to deploy into. No default: a wrong default deploys to the wrong project."

  validation {
    condition     = can(regex("^[a-z][a-z0-9-]{4,28}[a-z0-9]$", var.project_id))
    error_message = "project_id must be a valid GCP project id, not a project NUMBER or a display name."
  }
}

variable "region" {
  type        = string
  description = "Region for the Cloud Run services, e.g. us-central1. No default, for the same reason as project_id."
}

variable "name_prefix" {
  type        = string
  default     = "webhook-handler"
  description = "Prefix for every resource name, so two copies can share a project without colliding."

  validation {
    # Service account ids cap at 30 characters and the longest suffix below is
    # "-dead-letter-sub". Catching it here beats a failure deep inside a plan.
    condition     = can(regex("^[a-z][a-z0-9-]{0,12}[a-z0-9]$", var.name_prefix))
    error_message = "name_prefix must be lowercase letters, digits and hyphens, start with a letter, and be at most 14 characters."
  }
}

variable "labels" {
  type        = map(string)
  default     = {}
  description = "Labels applied to every resource that supports them. Useful for cost attribution."
}

variable "topic_retention" {
  type        = string
  default     = "86400s" # 24 hours
  description = <<-EOT
    How long the events topic itself retains messages, independently of any
    subscription. Enables seek-to-timestamp replay after an incident: without
    it, a subscription that acked a batch of poison messages cannot get them
    back once the bug is fixed. Costs storage; 10 minutes to 31 days.
  EOT
}

