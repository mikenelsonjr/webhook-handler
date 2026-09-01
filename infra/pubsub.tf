# The topic the receiver publishes to, the dead-letter topic that catches what
# the processor cannot handle, and a subscription on each.
#
# The push subscription that feeds the processor lands in #22, because its
# endpoint is the processor's URL and that service does not exist yet.

resource "google_pubsub_topic" "events" {
  name    = "${var.name_prefix}-events"
  labels  = var.labels
  project = var.project_id

  # How long the topic itself keeps messages, independently of any
  # subscription. Enables seek-to-timestamp for replay after an incident:
  # without it, a subscription that acked a batch of poison messages has no way
  # to get them back once the bug is fixed.
  message_retention_duration = var.topic_retention
}

resource "google_pubsub_topic" "dead_letter" {
  name    = "${var.name_prefix}-dead-letter"
  labels  = var.labels
  project = var.project_id
}

# The push subscription's dead-letter policy points here (#22). Messages the
# processor nacks `max_delivery_attempts` times are moved to the topic above.
#
# ---------------------------------------------------------------------------
# THIS SUBSCRIPTION IS THE POINT OF THE DEAD-LETTER TOPIC.
#
# Pub/Sub only retains a message for subscriptions that ALREADY EXISTED when it
# was published. A dead-letter topic with no subscription therefore retains
# nothing at all: failures are dutifully dead-lettered into a void, the DLQ
# stays empty, and that is indistinguishable from dead-lettering working
# correctly. You find out when someone asks to see the failures.
#
# It has no consumer, so it looks removable. It is not.
# ---------------------------------------------------------------------------
resource "google_pubsub_subscription" "dead_letter" {
  name    = "${var.name_prefix}-dead-letter-sub"
  topic   = google_pubsub_topic.dead_letter.id
  labels  = var.labels
  project = var.project_id

  # The maximum Pub/Sub allows. This exists to be read by a human who is not
  # watching, and a human who is not watching will not read it inside the
  # 7-day default either — but there is no reason to give them less.
  message_retention_duration = "604800s" # 7 days
  retain_acked_messages      = false

  # No expiration. A subscription with no subscriber is deleted after 31 days
  # by default, which would silently remove the thing that catches failures
  # precisely when nothing has failed for a month.
  expiration_policy {
    ttl = ""
  }
}

# ==============================================================================
# The push subscription: the thing that actually ties the two services
# together, and where four settings decide how the system behaves when things
# go wrong.
# ==============================================================================
resource "google_pubsub_subscription" "processor_push" {
  name    = "${var.name_prefix}-processor-push"
  topic   = google_pubsub_topic.events.id
  labels  = var.labels
  project = var.project_id

  # Above the handler's WORST case, not its median. Exceed it and the message
  # is redelivered while the first attempt is still running, so one event is
  # processed twice concurrently. That is the standard way a push subscriber
  # gets into trouble. var.request_timeout_seconds is validated to exceed this.
  ack_deadline_seconds = var.ack_deadline_seconds

  message_retention_duration = var.subscription_retention

  push_config {
    # Built from the service's own URL, never a hand-written copy: a copy
    # drifts silently the first time the service is renamed, and the symptom is
    # a 404 loop that looks like the processor being down.
    push_endpoint = "${google_cloud_run_v2_service.processor.uri}/_pubsub/push"

    oidc_token {
      service_account_email = google_service_account.push.email

      # Must match the service URL. Get this wrong and Cloud Run rejects every
      # delivery before the container sees it — a 403 loop indistinguishable
      # from a missing roles/run.invoker.
      audience = google_cloud_run_v2_service.processor.uri
    }
  }

  # Bounds how long a poison message churns. Without it, a message the handler
  # can never process is retried until it ages out of retention: days of noise
  # around one bad payload, and no record of it anywhere afterwards.
  dead_letter_policy {
    dead_letter_topic     = google_pubsub_topic.dead_letter.id
    max_delivery_attempts = var.max_delivery_attempts
  }

  # Explicit backoff, so a downstream outage is not turned into a self-inflicted
  # load test by a subscriber retrying flat out.
  retry_policy {
    minimum_backoff = "10s"
    maximum_backoff = "600s"
  }

  # A subscription with no subscriber is deleted after 31 days by default. This
  # one has a push endpoint rather than a puller, and there is no reason for it
  # to disappear during a quiet month.
  expiration_policy {
    ttl = ""
  }

  depends_on = [
    google_cloud_run_v2_service_iam_member.processor_push_only,
    google_service_account_iam_member.pubsub_mints_push_tokens,
  ]
}

# The third grant that fails silently, completing the set from iam.tf. Pub/Sub
# must be able to ACK a message on THIS subscription in order to move it to the
# dead-letter topic. Without it, dead-lettering is refused and the message
# retries forever — the same symptom as a missing publisher grant on the
# dead-letter topic, from the opposite side.
resource "google_pubsub_subscription_iam_member" "pubsub_acks_for_dead_lettering" {
  subscription = google_pubsub_subscription.processor_push.name
  role         = "roles/pubsub.subscriber"
  member       = local.pubsub_agent
  project      = var.project_id
}
