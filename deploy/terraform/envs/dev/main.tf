locals {
  account_id = var.aws_account_id
  region     = var.aws_region
  tags       = var.tags
}

module "docker_host" {
  source = "../../modules/aws/docker-host"

  name                 = var.name_prefix
  instance_type        = var.instance_type
  ami_id               = var.ami_id
  vpc_id               = var.vpc_id
  subnet_id            = var.subnet_id
  ssm_parameter_prefix = var.ssm_parameter_prefix

  # `unlimited` keeps a burstable instance from throttling to baseline once its CPU
  # credits run out, and a shrinking root volume is rejected mid-apply. The two
  # description strings are create-only in AWS, so they have to match an existing role
  # and security group byte for byte when an environment adopts live resources by import.
  cpu_credits                = "unlimited"
  root_volume_gb             = 30
  iam_role_description       = "Allows EC2 instances to call AWS services on your behalf."
  security_group_description = "dev backend - caddy 80/443"

  tags             = local.tags
  root_volume_tags = merge(local.tags, { Name = var.name_prefix })
}

data "aws_iam_policy_document" "ses_send" {
  statement {
    sid     = "SesSend"
    actions = ["ses:SendEmail", "ses:SendRawEmail"]
    # this one identity, not identity/*: the account also holds personal verified addresses
    resources = [aws_sesv2_email_identity.mail.arn]
  }
}

resource "aws_iam_role_policy" "ses_send" {
  name   = "${var.name_prefix}-ses-send"
  role   = module.docker_host.instance_role_name
  policy = data.aws_iam_policy_document.ses_send.json
}

data "aws_iam_policy_document" "export_s3" {
  statement {
    sid       = "ExportS3ObjectAccess"
    actions   = ["s3:PutObject", "s3:GetObject", "s3:DeleteObject", "s3:AbortMultipartUpload"]
    resources = ["${aws_s3_bucket.exports.arn}/*"]
  }
}

resource "aws_iam_role_policy" "export_s3" {
  name   = "${var.name_prefix}-export-s3"
  role   = module.docker_host.instance_role_name
  policy = data.aws_iam_policy_document.export_s3.json
}

resource "aws_s3_bucket" "exports" {
  # S3 bucket names are global across every AWS account, so the account id keeps this
  # unique without anyone having to invent a name
  bucket = "${var.name_prefix}-exports-${var.aws_account_id}"

  tags = local.tags
}

resource "aws_s3_bucket_public_access_block" "exports" {
  bucket = aws_s3_bucket.exports.id

  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_server_side_encryption_configuration" "exports" {
  bucket = aws_s3_bucket.exports.id

  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm = "AES256"
    }
  }
}

resource "aws_iam_openid_connect_provider" "github" {
  url            = "https://token.actions.githubusercontent.com"
  client_id_list = ["sts.amazonaws.com"]

  tags = local.tags
}

data "aws_iam_policy_document" "gha_deploy_trust" {
  statement {
    actions = ["sts:AssumeRoleWithWebIdentity"]

    principals {
      type        = "Federated"
      identifiers = [aws_iam_openid_connect_provider.github.arn]
    }

    condition {
      test     = "StringEquals"
      variable = "token.actions.githubusercontent.com:aud"
      values   = ["sts.amazonaws.com"]
    }

    condition {
      test     = "StringLike"
      variable = "token.actions.githubusercontent.com:sub"
      values   = ["repo:${var.github_repo}:ref:refs/heads/main"]
    }
  }
}

resource "aws_iam_role" "gha_deploy" {
  name               = "${var.name_prefix}-gha-deploy"
  assume_role_policy = data.aws_iam_policy_document.gha_deploy_trust.json

  tags = local.tags
}

data "aws_iam_policy_document" "gha_deploy" {
  statement {
    actions = ["ssm:SendCommand"]
    resources = [
      "arn:aws:ssm:${local.region}::document/AWS-RunShellScript",
      "arn:aws:ec2:${local.region}:${local.account_id}:instance/${module.docker_host.instance_id}",
    ]
  }

  statement {
    actions   = ["ssm:GetCommandInvocation", "ssm:ListCommandInvocations"]
    resources = ["*"]
  }
}

resource "aws_iam_role_policy" "gha_deploy" {
  name   = "SsmDeploy"
  role   = aws_iam_role.gha_deploy.name
  policy = data.aws_iam_policy_document.gha_deploy.json
}

# access key deliberately unmanaged: secret material, created/rotated out-of-band (SMTP password is a SigV4 derivative)
resource "aws_iam_user" "smtp" {
  name = "${var.name_prefix}-smtp"

  tags = local.tags
}

data "aws_iam_policy_document" "smtp_send" {
  statement {
    sid       = "SesSmtpSend"
    actions   = ["ses:SendRawEmail"]
    resources = [aws_sesv2_email_identity.mail.arn]
  }
}

resource "aws_iam_user_policy" "smtp_send" {
  name   = "${var.name_prefix}-smtp-send"
  user   = aws_iam_user.smtp.name
  policy = data.aws_iam_policy_document.smtp_send.json
}

resource "aws_sesv2_email_identity" "mail" {
  email_identity = var.mail_domain

  tags = local.tags
}
