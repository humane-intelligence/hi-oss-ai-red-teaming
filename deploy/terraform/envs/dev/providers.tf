provider "aws" {
  region = local.region
  # a stray AWS_PROFILE would otherwise apply this env into whatever account the credentials belong to
  allowed_account_ids = [local.account_id]
}
