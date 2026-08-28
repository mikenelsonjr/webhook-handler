# Every identity and every grant, in one file, so the blast radius of this
# deployment can be audited by reading a single page.
#
# THREE ACCOUNTS, NOT ONE
# -----------------------
# The receiver's runtime identity, the processor's runtime identity, and the
# PUSH identity Pub/Sub uses to call the processor. The push account can do
# exactly one thing — invoke the processor — and nothing else. Reusing a
# runtime account for push would hand any caller who obtains that token
# whatever the service itself can do, which for the receiver is publishing to
# the topic.
#
# Each grant below is on a specific resource, never at project level. A
# project-level roles/pubsub.publisher lets the receiver publish to every topic
# in the project, including the dead-letter topic it must never write to.

# The project number, for the Pub/Sub service agent below. Derived, not
# hardcoded: a literal project number makes this module un-cloneable, which
# defeats the point of the repo.
data "google_project" "this" {
  project_id = var.project_id
}

locals {
  # Google's own Pub/Sub identity. It is what mints push tokens and what moves
  # messages to a dead-letter topic — both on your behalf, using permissions
  # YOU grant it. It is not the same thing as the push service account below.
  pubsub_agent = "serviceAccount:service-${data.google_project.this.number}@gcp-sa-pubsub.iam.gserviceaccount.com"
}

# -- identities ----------------------------------------------------------------

resource "google_service_account" "ingest" {
  account_id   = "${var.name_prefix}-ingest"
  display_name = "Webhook receiver runtime identity"
  description  = "Runs the ingest Cloud Run service. Publishes to the events topic; reads the signing secret."
  project      = var.project_id
}

resource "google_service_account" "processor" {
  account_id   = "${var.name_prefix}-processor"
  display_name = "Webhook processor runtime identity"
  description  = "Runs the processor Cloud Run service. Needs whatever the handler touches, and nothing for Pub/Sub: push delivery requires no subscriber permission on the service side."
  project      = var.project_id
}

resource "google_service_account" "push" {
  account_id   = "${var.name_prefix}-push"
  display_name = "Pub/Sub push identity"
  description  = "The identity Pub/Sub presents when calling the processor. Invokes that one service and nothing else."
  project      = var.project_id
}

# -- the receiver's own permissions ---------------------------------------------

resource "google_pubsub_topic_iam_member" "ingest_publishes_events" {
  topic   = google_pubsub_topic.events.name
  role    = "roles/pubsub.publisher"
  member  = "serviceAccount:${google_service_account.ingest.email}"
  project = var.project_id
}

# ==============================================================================
# THE THREE GRANTS THAT FAIL SILENTLY
#
# None of these produce an error when missing. Delivery simply stops, or the
# dead-letter topic stays empty while the retry count climbs — which looks like
# a broken consumer, and sends you reading application logs that are fine.
#
# Two of them are grants to GOOGLE'S service agent rather than to anything you
# created, on resources you are not thinking about while wiring up a
# subscription. That is why they are missed.
# ==============================================================================

# 1. Without this, Pub/Sub cannot mint the OIDC token for the push request, so
#    the subscription NEVER DELIVERS. Nothing logs a reason.
resource "google_service_account_iam_member" "pubsub_mints_push_tokens" {
  service_account_id = google_service_account.push.name
  role               = "roles/iam.serviceAccountTokenCreator"
  member             = local.pubsub_agent
}

# 2. Without this, DEAD-LETTERING NEVER HAPPENS. A poison message is retried
#    until it ages out of retention, and the dead-letter topic you are watching
#    stays empty.
resource "google_pubsub_topic_iam_member" "pubsub_publishes_dead_letters" {
  topic   = google_pubsub_topic.dead_letter.name
  role    = "roles/pubsub.publisher"
  member  = local.pubsub_agent
  project = var.project_id
}

# 3. The same failure from the other side: Pub/Sub must be able to ACK the
#    message on the source subscription in order to move it. Granted on the
#    push subscription in #22, where that subscription exists.
