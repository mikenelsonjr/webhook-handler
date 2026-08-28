# The provider takes its credentials from the ambient environment —
# Application Default Credentials, a workload identity federation token in CI,
# or an impersonated service account. Nothing here reads a key file, and no
# credential belongs in this repo: it is public, and a key committed once is
# compromised in every fork and in GitHub's commit cache.
#
#   gcloud auth application-default login       # local
#   google-github-actions/auth@v2               # CI, via workload identity

provider "google" {
  project = var.project_id
  region  = var.region
}
