# Both services, from ONE image with different entrypoints. The receiver runs
# `main:app`, the subscriber runs `processor_main:app` — see the Dockerfile.
# Two images built from one repo drift; one image cannot.
#
# THE TWO SERVICES HAVE OPPOSITE AUTHENTICATION POSTURES, ON PURPOSE
# ------------------------------------------------------------------
# It reads as an inconsistency, so: the receiver is called by a provider that
# CANNOT present a Google identity, and the processor is called by Pub/Sub,
# which can. Whether an endpoint is public is decided by what its caller is
# capable of, not by preference.

resource "google_cloud_run_v2_service" "ingest" {
  name     = "${var.name_prefix}-ingest"
  location = var.region
  project  = var.project_id
  labels   = var.labels

  # Public: Aptly cannot authenticate, so the signing key is the only thing
  # between the internet and the topic. Restrict further at the network edge if
  # your provider publishes IP ranges.
  ingress = "INGRESS_TRAFFIC_ALL"

  deletion_protection = false

  template {
    service_account = google_service_account.ingest.email

    # Bounded so a burst — or a retry storm — cannot scale into a bill.
    scaling {
      min_instance_count = 0
      max_instance_count = var.ingest_max_instances
    }

    containers {
      image = var.ingest_image

      env {
        name  = "GCP_PROJECT"
        value = var.project_id
      }

      env {
        # The short name, not the fully-qualified id: that is what the
        # application's Settings expects. Taken from the resource rather than
        # rewritten as a literal, so a rename cannot half-apply.
        name  = "PUBSUB_TOPIC"
        value = google_pubsub_topic.events.name
      }

      env {
        # Mounted from Secret Manager, so the service needs no GCP client, no
        # IAM at request time, and gains no failure mode on the hot path.
        #
        # `latest` resolves at INSTANCE START, so a rotated secret takes effect
        # as instances recycle rather than immediately. That is the tradeoff
        # for keeping Secret Manager off the request path.
        name = "WEBHOOK_SIGNING_SECRET"
        value_source {
          secret_key_ref {
            secret  = google_secret_manager_secret.signing.secret_id
            version = "latest"
          }
        }
      }
    }
  }

  depends_on = [google_secret_manager_secret_iam_member.ingest_reads_signing]
}

resource "google_cloud_run_v2_service" "processor" {
  name     = "${var.name_prefix}-processor"
  location = var.region
  project  = var.project_id
  labels   = var.labels

  # Pub/Sub push originates outside the VPC, so this cannot be internal-only.
  # It is not open, though: there is no allUsers binding below, so every
  # request must carry a token from an identity holding roles/run.invoker.
  ingress = "INGRESS_TRAFFIC_ALL"

  deletion_protection = false

  template {
    service_account = google_service_account.processor.email

    scaling {
      min_instance_count = 0
      max_instance_count = var.processor_max_instances
    }

    # MUST EXCEED THE SUBSCRIPTION'S ACK DEADLINE (var.ack_deadline_seconds,
    # used in #22). If it does not, Cloud Run kills the request before Pub/Sub
    # gives up on it: the handler is cut off mid-work AND the message is
    # redelivered anyway — the worst of both.
    #
    # Enforced in the variable's own validation block rather than as a
    # precondition here, because variable validation runs before the provider
    # authenticates and is therefore checkable with no credentials.
    timeout = "${var.request_timeout_seconds}s"

    containers {
      image = var.processor_image

      env {
        # The platform authenticated the caller before this container saw the
        # request, so the application performs no verification of its own. See
        # processor/config.py.
        name  = "PUSH_AUTH_MODE"
        value = "iam"
      }
    }
  }
}

# -- who may invoke what --------------------------------------------------------

# PUBLIC, and correct. The provider cannot present a Google identity, so there
# is no token to require. An unset signing secret fails closed, so the failure
# mode is a service that rejects everything rather than one that accepts
# everything — but verify that rather than assume it.
resource "google_cloud_run_v2_service_iam_member" "ingest_public" {
  name     = google_cloud_run_v2_service.ingest.name
  location = google_cloud_run_v2_service.ingest.location
  project  = var.project_id
  role     = "roles/run.invoker"
  member   = "allUsers"
}

# NOT public. Only the push identity may call the processor, and Cloud Run
# validates its OIDC token before the request reaches the container — which is
# what PUSH_AUTH_MODE=iam above is asserting.
#
# Without this grant every push is refused with 403 and redelivered until the
# message ages out of retention. It is the least silent of the failures here,
# but only if you are looking at Cloud Run's logs rather than the processor's.
resource "google_cloud_run_v2_service_iam_member" "processor_push_only" {
  name     = google_cloud_run_v2_service.processor.name
  location = google_cloud_run_v2_service.processor.location
  project  = var.project_id
  role     = "roles/run.invoker"
  member   = "serviceAccount:${google_service_account.push.email}"
}
