# Everything that ties this environment to one AWS account, region, repository or
# domain. The four with no default — account, VPC, subnet, mail domain — cannot be
# guessed, so a plan fails fast until you supply them; see terraform.tfvars.example.

variable "aws_account_id" {
  description = "AWS account this environment applies into; providers.tf refuses to apply anywhere else"
  type        = string

  validation {
    condition     = can(regex("^[0-9]{12}$", var.aws_account_id))
    error_message = "aws_account_id must be a 12-digit AWS account id."
  }
}

variable "aws_region" {
  description = "Region for every resource in this environment"
  type        = string
  default     = "us-east-1"
}

variable "name_prefix" {
  description = "Prefix for every resource name here. IAM role and user names are account-global, so two environments in one account need different prefixes"
  type        = string
  default     = "ai-red-teaming-dev"
}

variable "vpc_id" {
  description = "VPC the security group is created in"
  type        = string

  validation {
    condition     = startswith(var.vpc_id, "vpc-")
    error_message = "vpc_id must be a vpc-… id."
  }
}

variable "subnet_id" {
  description = "Subnet to launch the instance in — must be public, the module attaches an Elastic IP"
  type        = string

  validation {
    condition     = startswith(var.subnet_id, "subnet-")
    error_message = "subnet_id must be a subnet-… id."
  }
}

variable "instance_type" {
  description = "EC2 instance type; the default is 2 vCPU / 4 GB, the footprint the monitoring runbook sizes its stack against"
  type        = string
  default     = "t3.medium"
}

variable "ami_id" {
  description = "AMI to launch. null resolves the newest Ubuntu 24.04 at plan time; AMI ids are region-scoped, so pin one only for your own region and only to keep rebuilds reproducible"
  type        = string
  default     = null
}

variable "ssm_parameter_prefix" {
  description = "SSM Parameter Store path the instance may read, without trailing slash"
  type        = string
  default     = "/aibackend/dev"
}

variable "mail_domain" {
  description = "Domain to register as an SES email identity. Nothing sends until you publish the DNS records AWS returns for it"
  type        = string
}

variable "github_repo" {
  description = "owner/repo allowed to assume the CD deploy role via OIDC, from refs/heads/main only. Point this at your own fork"
  type        = string
  default     = "humane-intelligence/hi-oss-ai-red-teaming"

  validation {
    condition     = can(regex("^[^/]+/[^/]+$", var.github_repo))
    error_message = "github_repo must be owner/repo."
  }
}

variable "tags" {
  description = "Tags applied to every resource in this environment"
  type        = map(string)
  default = {
    Project     = "ai-red-teaming"
    Environment = "dev"
  }
}
