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
