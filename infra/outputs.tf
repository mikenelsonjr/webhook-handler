# Outputs are how the later stories reference each other's resources instead of
# repeating string literals — the push subscription takes the processor's URL
# from here, not from a hand-written copy that drifts the first time a name
# changes.
#
# Resources arrive in issues #18 to #22; their outputs land alongside them.

output "project_id" {
  value       = var.project_id
  description = "The project everything above was created in. Echoed back so a plan output records it."
}

output "events_topic_id" {
  value       = google_pubsub_topic.events.id
  description = "Fully-qualified id of the events topic, for the dead-letter and push subscriptions."
}

output "events_topic_name" {
  value       = google_pubsub_topic.events.name
  description = "Short name, which is what the receiver's PUBSUB_TOPIC environment variable wants."
}

output "dead_letter_topic_id" {
  value       = google_pubsub_topic.dead_letter.id
  description = "Target of the push subscription's dead_letter_policy in #22."
}
