output "gcp_service_account" {
  description = "Annotate the service account with iam.gke.io/gcp-service-account = this value"
  value       = google_service_account.paa.email
}

output "database_url_secret" {
  description = "Secret Manager secret synced to Kubernetes as DATABASE_URL"
  value       = google_secret_manager_secret.database_url.secret_id
}

output "audit_bucket" {
  description = "Retention-locked bucket receiving audit chains"
  value       = google_storage_bucket.audit.name
}

output "integration_secrets" {
  description = "Secret Manager secrets the clearinghouse and messaging owners populate"
  value       = [for s in google_secret_manager_secret.integration : s.secret_id]
}
