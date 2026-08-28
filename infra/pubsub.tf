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
