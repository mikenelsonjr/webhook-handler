# Core rules plus the Google ruleset. The Google plugin is what catches an
# invalid region, a bad machine type, or a field the provider accepts and the
# API later rejects — the class of error `terraform validate` cannot see
# because validate only checks the schema, not the values.

config {
  call_module_type = "local"
}

plugin "terraform" {
  enabled = true
  preset  = "recommended"
}

plugin "google" {
  enabled = true
  version = "0.31.0"
  source  = "github.com/terraform-linters/tflint-ruleset-google"
}
