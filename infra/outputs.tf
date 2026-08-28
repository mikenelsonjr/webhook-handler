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

output "ingest_service_account" {
  value       = google_service_account.ingest.email
  description = "Runtime identity of the receiver. Publishes to the events topic; reads the signing secret."
}

output "processor_service_account" {
  value       = google_service_account.processor.email
  description = "Runtime identity of the processor. Grant it whatever your handler touches."
}

output "push_service_account" {
  value       = google_service_account.push.email
  description = "The identity Pub/Sub presents when calling the processor. Separate from both runtime identities on purpose."
}

output "pubsub_service_agent" {
  value       = trimprefix(local.pubsub_agent, "serviceAccount:")
  description = "Google's own Pub/Sub identity. Output because two of the grants that fail silently are made TO it, and knowing the address is half of debugging them."
}

output "signing_secret_id" {
  value       = google_secret_manager_secret.signing.secret_id
  description = "Short id of the signing secret. Add the first VERSION out of band; Terraform deliberately does not manage one."
}

output "ingest_url" {
  value       = google_cloud_run_v2_service.ingest.uri
  description = "Public URL of the receiver. This is the endpoint you give the webhook provider."
}

output "processor_url" {
  value       = google_cloud_run_v2_service.processor.uri
  description = "URL of the processor. The push subscription in #22 builds its endpoint and its OIDC audience from this, rather than from a hand-written copy."
}
