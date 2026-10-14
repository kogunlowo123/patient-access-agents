terraform {
  required_version = ">= 1.7.0"
  required_providers {
    azurerm = { source = "hashicorp/azurerm", version = "~> 4.5" }
    random  = { source = "hashicorp/random", version = "~> 3.6" }
  }
}

provider "azurerm" {
  features {
    key_vault {
      purge_soft_delete_on_destroy = false
    }
  }
  subscription_id = var.subscription_id
}

data "azurerm_client_config" "current" {}

data "azurerm_resource_group" "paa" {
  name = var.resource_group_name
}

locals {
  name     = var.name
  location = data.azurerm_resource_group.paa.location
  # Storage and Key Vault names are global; derive a stable unique suffix.
  suffix = substr(sha1("${data.azurerm_client_config.current.subscription_id}${local.name}"), 0, 8)
}

# ------------------------------------------------------------------ database

resource "random_password" "db" {
  length  = 40
  special = false
}

resource "azurerm_postgresql_flexible_server" "paa" {
  name                          = "${local.name}-${local.suffix}"
  resource_group_name           = data.azurerm_resource_group.paa.name
  location                      = local.location
  version                       = "16"
  sku_name                      = var.db_sku
  storage_mb                    = var.db_storage_mb
  delegated_subnet_id           = var.db_subnet_id
  private_dns_zone_id           = var.db_private_dns_zone_id
  public_network_access_enabled = false
  administrator_login           = "paa"
  administrator_password        = random_password.db.result
  backup_retention_days         = 14
  geo_redundant_backup_enabled  = true
  zone                          = "1"
  high_availability {
    mode                      = "ZoneRedundant"
    standby_availability_zone = "2"
  }
  tags = var.tags
}

resource "azurerm_postgresql_flexible_server_configuration" "require_tls" {
  name      = "require_secure_transport"
  server_id = azurerm_postgresql_flexible_server.paa.id
  value     = "on"
}

resource "azurerm_postgresql_flexible_server_database" "paa" {
  name      = "paa"
  server_id = azurerm_postgresql_flexible_server.paa.id
  charset   = "UTF8"
  collation = "en_US.utf8"
}

# ----------------------------------------------------------------- key vault

resource "azurerm_key_vault" "paa" {
  name                          = "kv-${substr(local.name, 0, 12)}-${local.suffix}"
  resource_group_name           = data.azurerm_resource_group.paa.name
  location                      = local.location
  tenant_id                     = data.azurerm_client_config.current.tenant_id
  sku_name                      = "premium"
  rbac_authorization_enabled    = true
  purge_protection_enabled      = true
  soft_delete_retention_days    = 90
  public_network_access_enabled = false
  network_acls {
    default_action = "Deny"
    bypass         = "AzureServices"
  }
  tags = var.tags
}

resource "azurerm_key_vault_secret" "database_url" {
  name         = "${local.name}-database-url"
  key_vault_id = azurerm_key_vault.paa.id
  value = format(
    "postgresql+asyncpg://paa:%s@%s:5432/paa?ssl=require",
    random_password.db.result, azurerm_postgresql_flexible_server.paa.fqdn,
  )
  content_type = "sqlalchemy-url"
}

# ------------------------------------------------------------- audit (WORM)

resource "azurerm_storage_account" "audit" {
  name                            = "staudit${local.suffix}"
  resource_group_name             = data.azurerm_resource_group.paa.name
  location                        = local.location
  account_tier                    = "Standard"
  account_replication_type        = "GZRS"
  min_tls_version                 = "TLS1_2"
  https_traffic_only_enabled      = true
  allow_nested_items_to_be_public = false
  shared_access_key_enabled       = false
  public_network_access_enabled   = false
  network_rules {
    default_action = "Deny"
    bypass         = ["AzureServices"]
  }
  blob_properties {
    versioning_enabled = true
  }
  immutability_policy {
    allow_protected_append_writes = true
    state                         = "Unlocked"
    period_since_creation_in_days = var.audit_retention_days
  }
  tags = var.tags
}

resource "azurerm_storage_container" "audit" {
  name                  = "audit-chains"
  storage_account_id    = azurerm_storage_account.audit.id
  container_access_type = "private"
}

# ---------------------------------------------- workload identity (AKS)

resource "azurerm_user_assigned_identity" "paa" {
  name                = local.name
  resource_group_name = data.azurerm_resource_group.paa.name
  location            = local.location
  tags                = var.tags
}

resource "azurerm_federated_identity_credential" "paa" {
  name                = "${local.name}-aks"
  resource_group_name = data.azurerm_resource_group.paa.name
  parent_id           = azurerm_user_assigned_identity.paa.id
  issuer              = var.aks_oidc_issuer_url
  subject             = "system:serviceaccount:${var.namespace}:${var.service_account}"
  audience            = ["api://AzureADTokenExchange"]
}

resource "azurerm_role_assignment" "audit_writer" {
  scope                = azurerm_storage_container.audit.id
  role_definition_name = "Storage Blob Data Contributor"
  principal_id         = azurerm_user_assigned_identity.paa.principal_id
}
