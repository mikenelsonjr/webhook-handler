# The signing secret the receiver compares inbound requests against.
#
# ==============================================================================
# TERRAFORM CREATES THE CONTAINER. IT MUST NEVER CREATE THE VALUE.
#
# There is no `google_secret_manager_secret_version` here, and adding one with
# a real `secret_data` would be a security regression, not a convenience.
#
# Terraform state records every attribute of every resource it manages, in
# plaintext, including anything a provider marks sensitive. A version resource
# therefore writes the signing key into state — so the secret now lives
# wherever state lives, in every backup of it, in every local copy anyone has
# pulled, and in the scrollback of anyone who has run `terraform show`. That is
# strictly worse than not using Secret Manager at all, because it looks
# managed.
#
# The first version goes in out of band, once:
#
#   printf '%s' "$SIGNING_KEY" | \
#     gcloud secrets versions add webhook-handler-signing-secret --data-file=-
#
# `printf`, not `echo`: echo appends a newline, and a trailing newline in a
# shared secret produces a comparison failure that looks exactly like a wrong
# key.
# ==============================================================================

resource "google_secret_manager_secret" "signing" {
  secret_id = "${var.name_prefix}-signing-secret"
  project   = var.project_id
  labels    = var.labels

  replication {
    auto {}
  }
}

# The receiver reads this ONE secret. Not project-level: a project-wide
# secretAccessor would let a compromised receiver read every secret in the
# project, which for most projects is the database password.
resource "google_secret_manager_secret_iam_member" "ingest_reads_signing" {
  secret_id = google_secret_manager_secret.signing.id
  role      = "roles/secretmanager.secretAccessor"
  member    = "serviceAccount:${google_service_account.ingest.email}"
  project   = var.project_id
}
