# Version constraints, pinned to compatible-release ranges rather than left
# open. A bare `>= 5.0` lets a major provider release land in a `terraform
# init` on a Tuesday and change what `plan` says about resources nobody
# touched. This is the same lesson as issue #4 on the Python side, where a
# wildcard let pip backtrack to a 2015 release of a package.
#
# `~> 6.0` means >= 6.0, < 7.0: patches and minors arrive, majors do not.

terraform {
  required_version = "~> 1.9"

  required_providers {
    google = {
      source  = "hashicorp/google"
      version = "~> 6.0"
    }
  }

  # No backend block. State location is the consumer's decision and depends on
  # a bucket this module does not create — declaring one here would make
  # `terraform init` fail for everyone who clones the template. Configure it
  # with `-backend-config`, or add a backend block in your own copy:
  #
  #   terraform {
  #     backend "gcs" {
  #       bucket = "my-tf-state"
  #       prefix = "webhook-handler"
  #     }
  #   }
  #
  # Local state is fine for a first look and wrong for anything shared: it
  # holds every attribute of every resource, in plaintext, on one laptop.
}
