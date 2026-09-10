terraform {
  # >= 1.10: native S3 state locking (use_lockfile) — experimental in Terraform 1.10,
  # GA in 1.11 (which deprecates dynamodb_table); OpenTofu carries it from 1.10
  required_version = ">= 1.10"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 6.0"
    }
  }
}
