# Partial backend configuration: the state bucket is globally unique and the region is
# yours, so neither can be committed here — and backend blocks take no expressions, so
# they cannot read a variable either. Both are supplied at init time:
#
#   cp backend.hcl.example backend.hcl   # edit it, it is gitignored
#   terraform init -backend-config=backend.hcl
#
# `terraform init -backend=false` (what CI's Terraform check runs) skips this entirely.
terraform {
  backend "s3" {
    key          = "dev/terraform.tfstate"
    encrypt      = true
    use_lockfile = true
  }
}
