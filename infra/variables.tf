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
    # Service account ids cap at 30 characters, and the longest suffix applied
    # to this prefix is "-processor" (10). So 20 is the real bound — the
    # subscription suffixes are longer but subscription names allow 255.
    #
    # An earlier version of this capped at 14, derived from the wrong suffix,
    # which made the DEFAULT VALUE fail its own validation. `terraform
    # validate` does not catch that; only `plan` evaluates variable validation.
    condition     = can(regex("^[a-z][a-z0-9-]{0,18}[a-z0-9]$", var.name_prefix))
    error_message = "name_prefix must be lowercase letters, digits and hyphens, start with a letter, and be at most 20 characters (service account ids cap at 30, and the longest suffix is -processor)."
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


variable "ingest_image" {
  type        = string
  description = "Container image for the receiver, e.g. us-central1-docker.pkg.dev/PROJECT/repo/webhook-handler:TAG. Use a digest or an immutable tag; :latest makes a rollback ambiguous."
}

variable "processor_image" {
  type        = string
  description = "Container image for the processor. Same image as ingest in the normal case — the Dockerfile ships both entrypoints — with a different command."
}

variable "ingest_max_instances" {
  type        = number
  default     = 10
  description = "Upper bound on receiver instances, so a burst cannot scale into a bill."
}

variable "processor_max_instances" {
  type        = number
  default     = 10
  description = "Upper bound on processor instances. Also caps concurrent duplicate handling during a redelivery storm."
}

variable "ack_deadline_seconds" {
  type        = number
  default     = 60
  description = <<-EOT
    How long Pub/Sub waits for the processor to answer a push before treating
    the message as unacknowledged. Set this above the handler's WORST case, not
    its median: exceed it and the message is redelivered while the first
    attempt is still running, so one event is processed twice concurrently.
    Must be less than request_timeout_seconds. Range 10-600.
  EOT

  validation {
    condition     = var.ack_deadline_seconds >= 10 && var.ack_deadline_seconds <= 600
    error_message = "ack_deadline_seconds must be between 10 and 600."
  }
}

variable "request_timeout_seconds" {
  type        = number
  default     = 120
  description = <<-EOT
    Cloud Run request timeout for the processor. MUST exceed
    ack_deadline_seconds, or Cloud Run kills the request before Pub/Sub gives
    up on it: the handler is cut off mid-work and the message is redelivered
    anyway — the worst of both.
  EOT

  validation {
    # Cross-variable validation, which Terraform 1.9 allows. Deliberately here
    # rather than as a `precondition` on the Cloud Run resource: variable
    # validation runs BEFORE the provider authenticates, so this invariant is
    # checkable with no GCP credentials at all. A precondition on the resource
    # is never reached in a plan that cannot authenticate, which made it
    # unverifiable in CI and on a machine with no project.
    condition     = var.request_timeout_seconds > var.ack_deadline_seconds
    error_message = "request_timeout_seconds must exceed ack_deadline_seconds, or Cloud Run kills the request before Pub/Sub gives up on it and the message is redelivered while the first attempt is still running."
  }
}

variable "subscription_retention" {
  type        = string
  default     = "604800s" # 7 days, the maximum
  description = "How long the push subscription retains unacknowledged messages. The maximum by default: a message that cannot be delivered for a week is a message you want to still have."
}

variable "max_delivery_attempts" {
  type        = number
  default     = 5
  description = <<-EOT
    Deliveries before a message is moved to the dead-letter topic. Bounds how
    long a poison message churns; without it, one the handler can never process
    is retried until it ages out of retention. Range 5-100.
  EOT

  validation {
    condition     = var.max_delivery_attempts >= 5 && var.max_delivery_attempts <= 100
    error_message = "max_delivery_attempts must be between 5 and 100 (Pub/Sub's own range)."
  }
}
