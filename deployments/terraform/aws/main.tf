terraform {
  required_version = ">= 1.7.0"
  required_providers {
    aws    = { source = "hashicorp/aws", version = "~> 5.70" }
    random = { source = "hashicorp/random", version = "~> 3.6" }
  }
}

provider "aws" {
  region = var.region
  default_tags { tags = var.tags }
}

data "aws_caller_identity" "current" {}

locals {
  name            = var.name
  oidc_issuer     = trimprefix(var.eks_oidc_issuer_url, "https://")
  service_account = "system:serviceaccount:${var.namespace}:${var.service_account}"
}

# ---------------------------------------------------------------- encryption

resource "aws_kms_key" "paa" {
  description             = "${local.name}: database, secrets and audit encryption"
  enable_key_rotation     = true
  deletion_window_in_days = 30
}

resource "aws_kms_alias" "paa" {
  name          = "alias/${local.name}"
  target_key_id = aws_kms_key.paa.key_id
}

# ------------------------------------------------------------------ database

resource "aws_db_subnet_group" "paa" {
  name       = local.name
  subnet_ids = var.private_subnet_ids
}

resource "aws_security_group" "db" {
  name        = "${local.name}-db"
  description = "PostgreSQL access from the EKS cluster only"
  vpc_id      = var.vpc_id
}

resource "aws_vpc_security_group_ingress_rule" "db_from_cluster" {
  security_group_id            = aws_security_group.db.id
  referenced_security_group_id = var.cluster_security_group_id
  ip_protocol                  = "tcp"
  from_port                    = 5432
  to_port                      = 5432
  description                  = "EKS nodes"
}

resource "aws_db_parameter_group" "paa" {
  name   = local.name
  family = "postgres16"
  parameter {
    name  = "rds.force_ssl"
    value = "1"
  }
  parameter {
    name  = "log_connections"
    value = "1"
  }
}

resource "random_password" "db" {
  length  = 40
  special = false
}

resource "aws_db_instance" "paa" {
  identifier                          = local.name
  engine                              = "postgres"
  engine_version                      = "16.4"
  instance_class                      = var.db_instance_class
  allocated_storage                   = var.db_allocated_storage_gb
  max_allocated_storage               = var.db_allocated_storage_gb * 4
  storage_type                        = "gp3"
  storage_encrypted                   = true
  kms_key_id                          = aws_kms_key.paa.arn
  db_name                             = "paa"
  username                            = "paa"
  password                            = random_password.db.result
  db_subnet_group_name                = aws_db_subnet_group.paa.name
  vpc_security_group_ids              = [aws_security_group.db.id]
  parameter_group_name                = aws_db_parameter_group.paa.name
  multi_az                            = true
  publicly_accessible                 = false
  backup_retention_period             = 14
  deletion_protection                 = true
  skip_final_snapshot                 = false
  final_snapshot_identifier           = "${local.name}-final"
  copy_tags_to_snapshot               = true
  auto_minor_version_upgrade          = true
  performance_insights_enabled        = true
  performance_insights_kms_key_id     = aws_kms_key.paa.arn
  iam_database_authentication_enabled = true
  enabled_cloudwatch_logs_exports     = ["postgresql"]
}

resource "aws_secretsmanager_secret" "database_url" {
  name       = "${local.name}-database-url"
  kms_key_id = aws_kms_key.paa.arn
}

resource "aws_secretsmanager_secret_version" "database_url" {
  secret_id = aws_secretsmanager_secret.database_url.id
  secret_string = format(
    "postgresql+asyncpg://paa:%s@%s:%d/paa?ssl=require",
    random_password.db.result, aws_db_instance.paa.address, aws_db_instance.paa.port,
  )
}

# ------------------------------------------------------------- audit (WORM)

resource "aws_s3_bucket" "audit" {
  bucket              = "${local.name}-audit-${data.aws_caller_identity.current.account_id}"
  object_lock_enabled = true
}

resource "aws_s3_bucket_versioning" "audit" {
  bucket = aws_s3_bucket.audit.id
  versioning_configuration { status = "Enabled" }
}

resource "aws_s3_bucket_object_lock_configuration" "audit" {
  bucket = aws_s3_bucket.audit.id
  rule {
    default_retention {
      mode = "COMPLIANCE"
      days = var.audit_retention_days
    }
  }
}

resource "aws_s3_bucket_server_side_encryption_configuration" "audit" {
  bucket = aws_s3_bucket.audit.id
  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm     = "aws:kms"
      kms_master_key_id = aws_kms_key.paa.arn
    }
    bucket_key_enabled = true
  }
}

resource "aws_s3_bucket_public_access_block" "audit" {
  bucket                  = aws_s3_bucket.audit.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

data "aws_iam_policy_document" "audit_bucket" {
  statement {
    sid       = "DenyInsecureTransport"
    effect    = "Deny"
    actions   = ["s3:*"]
    resources = [aws_s3_bucket.audit.arn, "${aws_s3_bucket.audit.arn}/*"]
    principals {
      type        = "*"
      identifiers = ["*"]
    }
    condition {
      test     = "Bool"
      variable = "aws:SecureTransport"
      values   = ["false"]
    }
  }
}

resource "aws_s3_bucket_policy" "audit" {
  bucket = aws_s3_bucket.audit.id
  policy = data.aws_iam_policy_document.audit_bucket.json
}

# ---------------------------------------------------- workload identity (IRSA)

data "aws_iam_policy_document" "trust" {
  statement {
    actions = ["sts:AssumeRoleWithWebIdentity"]
    principals {
      type        = "Federated"
      identifiers = [var.eks_oidc_provider_arn]
    }
    condition {
      test     = "StringEquals"
      variable = "${local.oidc_issuer}:sub"
      values   = [local.service_account]
    }
    condition {
      test     = "StringEquals"
      variable = "${local.oidc_issuer}:aud"
      values   = ["sts.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "paa" {
  name               = local.name
  assume_role_policy = data.aws_iam_policy_document.trust.json
}

data "aws_iam_policy_document" "paa" {
  statement {
    sid       = "ShipAuditChains"
    actions   = ["s3:PutObject"]
    resources = ["${aws_s3_bucket.audit.arn}/*"]
  }
  statement {
    sid       = "EncryptAudit"
    actions   = ["kms:GenerateDataKey", "kms:Decrypt"]
    resources = [aws_kms_key.paa.arn]
  }
}

resource "aws_iam_role_policy" "paa" {
  role   = aws_iam_role.paa.id
  policy = data.aws_iam_policy_document.paa.json
}

# ------------------------------------------- integration secrets (values set
# by the clearinghouse and messaging owners; see docs/runbooks/startup.md)

resource "aws_secretsmanager_secret" "integration" {
  for_each   = toset(["clearinghouse-api-key", "notification-signing-key"])
  name       = "${local.name}-${each.key}"
  kms_key_id = aws_kms_key.paa.arn
}
