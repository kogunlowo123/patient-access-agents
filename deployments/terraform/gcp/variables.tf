variable "name" {
  description = "Resource name prefix"
  type        = string
  default     = "patient-access-agents"
}

variable "project_id" {
  description = "GCP project"
  type        = string
}

variable "region" {
  description = "GCP region"
  type        = string
}

variable "network_id" {
  description = "VPC network self link with private services access configured"
  type        = string
}

variable "db_tier" {
  description = "Cloud SQL machine tier"
  type        = string
  default     = "db-custom-4-16384"
}

variable "namespace" {
  description = "Kubernetes namespace of the service"
  type        = string
  default     = "patient-access"
}

variable "service_account" {
  description = "Kubernetes service account of the service"
  type        = string
  default     = "patient-access-agents"
}

variable "audit_retention_days" {
  description = "Bucket retention for audit chains"
  type        = number
  default     = 2555
}

variable "lock_audit_retention" {
  description = "Permanently lock the retention policy (irreversible)"
  type        = bool
  default     = false
}
