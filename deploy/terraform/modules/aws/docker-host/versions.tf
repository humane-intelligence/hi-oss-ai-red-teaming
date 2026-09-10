terraform {
  # kept in step with the env roots, which need >= 1.10 for their state backend
  required_version = ">= 1.10"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 6.0"
    }
  }
}
