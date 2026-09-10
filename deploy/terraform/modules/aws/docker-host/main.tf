data "aws_partition" "current" {}

data "aws_region" "current" {}

data "aws_caller_identity" "current" {}

data "aws_ami" "this" {
  # skipped when the consumer pins an id: an unconditional lookup fails the whole
  # plan once the name pattern stops matching a published image
  count = var.ami_id == null ? 1 : 0

  most_recent = true
  owners      = [var.ami_owner]

  filter {
    name   = "name"
    values = [var.ami_name_pattern]
  }
}

resource "aws_security_group" "this" {
  name        = "${var.name}-sg"
  description = var.security_group_description
  vpc_id      = var.vpc_id

  tags = var.tags
}

resource "aws_vpc_security_group_ingress_rule" "http" {
  security_group_id = aws_security_group.this.id
  cidr_ipv4         = "0.0.0.0/0"
  from_port         = 80
  to_port           = 80
  ip_protocol       = "tcp"
}

resource "aws_vpc_security_group_ingress_rule" "https" {
  security_group_id = aws_security_group.this.id
  cidr_ipv4         = "0.0.0.0/0"
  from_port         = 443
  to_port           = 443
  ip_protocol       = "tcp"
}

resource "aws_vpc_security_group_egress_rule" "all" {
  security_group_id = aws_security_group.this.id
  cidr_ipv4         = "0.0.0.0/0"
  ip_protocol       = "-1"
}

data "aws_iam_policy_document" "assume_role" {
  statement {
    actions = ["sts:AssumeRole"]

    principals {
      type        = "Service"
      identifiers = ["ec2.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "this" {
  name               = "${var.name}-ec2"
  description        = var.iam_role_description
  assume_role_policy = data.aws_iam_policy_document.assume_role.json

  tags = var.tags
}

resource "aws_iam_role_policy_attachment" "ssm_core" {
  role       = aws_iam_role.this.name
  policy_arn = "arn:${data.aws_partition.current.partition}:iam::aws:policy/AmazonSSMManagedInstanceCore"
}

data "aws_iam_policy_document" "param_store_read" {
  statement {
    actions   = ["ssm:GetParameter", "ssm:GetParametersByPath"]
    resources = ["arn:${data.aws_partition.current.partition}:ssm:${data.aws_region.current.region}:${data.aws_caller_identity.current.account_id}:parameter${var.ssm_parameter_prefix}/*"]
  }

  statement {
    actions   = ["kms:Decrypt"]
    resources = ["*"]

    condition {
      test     = "StringEquals"
      variable = "kms:ViaService"
      values   = ["ssm.${data.aws_region.current.region}.amazonaws.com"]
    }
  }
}

resource "aws_iam_role_policy" "param_store_read" {
  name   = "ParamStoreRead"
  role   = aws_iam_role.this.name
  policy = data.aws_iam_policy_document.param_store_read.json
}

resource "aws_iam_instance_profile" "this" {
  name = "${var.name}-ec2"
  role = aws_iam_role.this.name
}

resource "aws_instance" "this" {
  ami                         = var.ami_id != null ? var.ami_id : data.aws_ami.this[0].id
  instance_type               = var.instance_type
  subnet_id                   = var.subnet_id
  vpc_security_group_ids      = [aws_security_group.this.id]
  iam_instance_profile        = aws_iam_instance_profile.this.name
  associate_public_ip_address = true

  credit_specification {
    cpu_credits = var.cpu_credits
  }

  root_block_device {
    volume_size = var.root_volume_gb
    volume_type = "gp3"
    encrypted   = true
    tags        = var.root_volume_tags
  }

  metadata_options {
    http_tokens = "required"
    # a docker bridge adds one hop; the AWS default of 1 makes IMDSv2 tokens unreachable from containers
    http_put_response_hop_limit = 2
  }

  tags = merge(var.tags, { Name = var.name })

  lifecycle {
    # adopted instance: an unpinned AMI resolving to a newer image forces replacement, and
    # the bootstrap user_data this module does not manage would be cleared (stop/modify/start)
    ignore_changes = [ami, user_data]
    # root-volume data is not replicated, so a replacement loses it
    prevent_destroy = true
  }
}

resource "aws_eip" "this" {
  domain = "vpc"

  tags = merge(var.tags, { Name = "${var.name}-eip" })

  lifecycle {
    # the address is what DNS points at; releasing it is not reversible
    prevent_destroy = true
  }
}

resource "aws_eip_association" "this" {
  allocation_id = aws_eip.this.id
  instance_id   = aws_instance.this.id
}
