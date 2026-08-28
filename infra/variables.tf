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

# `name_prefix` and `labels` arrive in #18, with the first resources that use
# them. A variable with no consumer is dead config, and tflint is right to say
# so — keeping the lint clean from the first commit is what makes a warning
# later mean something.
