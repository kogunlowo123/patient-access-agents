terraform {
  required_version = ">= 1.7.0"
  required_providers {
    google      = { source = "hashicorp/google", version = "~> 6.8" }
    google-beta = { source = "hashicorp/google-beta", version = "~> 6.8" }
    random      = { source = "hashicorp/random", version = "~> 3.6" }
  }
}

provider "google" {
  project = var.project_id
  region  = var.region
}

provider "google-beta" {
  project = var.project_id
  region  = var.region
}

locals {
  name = var.name
}

# ---------------------------------------------------------------- encryption

resource "google_kms_key_ring" "paa" {
  name     = local.name
  location = var.region
}

resource "google_kms_crypto_key" "paa" {
  name            = "${local.name}-data"
  key_ring        = google_kms_key_ring.paa.id
  rotation_period = "7776000s" # 90 days
  lifecycle {
    prevent_destroy = true
  }
}

data "google_project" "current" {}

resource "google_project_service_identity" "sql" {
  provider = google-beta
  project  = var.project_id
  service  = "sqladmin.googleapis.com"
}

resource "google_kms_crypto_key_iam_member" "sql" {
  crypto_key_id = google_kms_crypto_key.paa.id
  role          = "roles/cloudkms.cryptoKeyEncrypterDecrypter"
  member        = "serviceAccount:${google_project_service_identity.sql.email}"
}

resource "google_kms_crypto_key_iam_member" "storage" {
  crypto_key_id = google_kms_crypto_key.paa.id
  role          = "roles/cloudkms.cryptoKeyEncrypterDecrypter"
  member        = "serviceAccount:service-${data.google_project.current.number}@gs-project-accounts.iam.gserviceaccount.com"
}

# ------------------------------------------------------------------ database

resource "random_password" "db" {
  length  = 40
  special = false
}

resource "google_sql_database_instance" "paa" {
  name                = local.name
  database_version    = "POSTGRES_16"
  region              = var.region
  encryption_key_name = google_kms_crypto_key.paa.id
  deletion_protection = true
  settings {
    tier              = var.db_tier
    availability_type = "REGIONAL"
    disk_autoresize   = true
    ip_configuration {
      ipv4_enabled                                  = false
      private_network                               = var.network_id
      ssl_mode                                      = "ENCRYPTED_ONLY"
      enable_private_path_for_google_cloud_services = true
    }
    backup_configuration {
      enabled                        = true
      point_in_time_recovery_enabled = true
      backup_retention_settings {
        retained_backups = 14
      }
    }
    database_flags {
      name  = "log_connections"
      value = "on"
    }
  }
  depends_on = [google_kms_crypto_key_iam_member.sql]
}

resource "google_sql_database" "paa" {
  name     = "paa"
  instance = google_sql_database_instance.paa.name
}

resource "google_sql_user" "paa" {
  name     = "paa"
  instance = google_sql_database_instance.paa.name
  password = random_password.db.result
}

resource "google_secret_manager_secret" "database_url" {
  secret_id = "${local.name}-database-url"
  replication {
    user_managed {
      replicas {
        location = var.region
      }
    }
  }
}

resource "google_secret_manager_secret_version" "database_url" {
  secret = google_secret_manager_secret.database_url.id
  secret_data = format(
    "postgresql+asyncpg://paa:%s@%s:5432/paa?ssl=require",
    random_password.db.result, google_sql_database_instance.paa.private_ip_address,
  )
}

# ------------------------------------------------------------- audit (WORM)

resource "google_storage_bucket" "audit" {
  name                        = "${local.name}-audit-${var.project_id}"
  location                    = var.region
  uniform_bucket_level_access = true
  public_access_prevention    = "enforced"
  versioning {
    enabled = true
  }
  retention_policy {
    retention_period = var.audit_retention_days * 86400
    is_locked        = var.lock_audit_retention
  }
  encryption {
    default_kms_key_name = google_kms_crypto_key.paa.id
  }
  depends_on = [google_kms_crypto_key_iam_member.storage]
}

# --------------------------------------------- workload identity (GKE)

resource "google_service_account" "paa" {
  account_id   = substr(local.name, 0, 30)
  display_name = "Agent identity paa"
}

resource "google_service_account_iam_member" "workload_identity" {
  service_account_id = google_service_account.paa.name
  role               = "roles/iam.workloadIdentityUser"
  member             = "serviceAccount:${var.project_id}.svc.id.goog[${var.namespace}/${var.service_account}]"
}

resource "google_storage_bucket_iam_member" "audit_writer" {
  bucket = google_storage_bucket.audit.name
  role   = "roles/storage.objectCreator"
  member = "serviceAccount:${google_service_account.paa.email}"
}

# ------------------------------------------------- Vertex AI (Gemini drafting)

resource "google_project_iam_member" "vertex_user" {
  project = var.project_id
  role    = "roles/aiplatform.user"
  member  = "serviceAccount:${google_service_account.paa.email}"
}

# ------------------------------------------- integration secrets (values set
# by the clearinghouse and messaging owners; see docs/runbooks/startup.md)

resource "google_secret_manager_secret" "integration" {
  for_each  = toset(["clearinghouse-api-key", "notification-signing-key"])
  secret_id = "${local.name}-${each.key}"
  replication {
    user_managed {
      replicas {
        location = var.region
      }
    }
  }
}
