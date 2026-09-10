variable "ami_id" {
  description = "AMI to launch; null resolves the newest image matching ami_name_pattern (pin it to keep rebuilds reproducible)"
  type        = string
  default     = null

  validation {
    # ternary, not ||: TF < 1.12 doesn't short-circuit and would crash on the null default
    condition     = var.ami_id == null ? true : startswith(var.ami_id, "ami-")
    error_message = "ami_id must be an ami-… id, or null to resolve ami_name_pattern."
  }
}

variable "ami_name_pattern" {
  description = "AMI name filter for the instance image"
  type        = string
  default     = "ubuntu/images/hvm-ssd-gp3/ubuntu-noble-24.04-amd64-server-*"
}

variable "ami_owner" {
  description = "AWS account id owning the AMI (default: Canonical)"
  type        = string
  default     = "099720109477"
}

variable "cpu_credits" {
  description = "Credit option for burstable (t-family) instances"
  type        = string
  default     = "standard"

  validation {
    condition     = contains(["standard", "unlimited"], var.cpu_credits)
    error_message = "cpu_credits must be standard or unlimited."
  }
}

variable "iam_role_description" {
  description = "Description of the instance IAM role (console-created roles carry AWS boilerplate — match it when importing)"
  type        = string
  default     = "Instance role for the docker host"
}

variable "instance_type" {
  description = "EC2 instance type"
  type        = string
}

variable "name" {
  description = "Name prefix for all resources (also the instance Name tag)"
  type        = string
}

variable "root_volume_gb" {
  description = "Root EBS volume size in GB (gp3, encrypted)"
  type        = number
  default     = 30
}

variable "root_volume_tags" {
  description = "Tags for the root EBS volume"
  type        = map(string)
  default     = {}
}

variable "security_group_description" {
  description = "Security group description; create-only in AWS, so it must match the existing value when adopting a live SG via import"
  type        = string
  default     = "HTTP/HTTPS ingress for the docker host"
}

variable "ssm_parameter_prefix" {
  description = "SSM Parameter Store path the instance may read, without trailing slash (e.g. /myapp/dev)"
  type        = string

  validation {
    condition     = startswith(var.ssm_parameter_prefix, "/") && !endswith(var.ssm_parameter_prefix, "/")
    error_message = "ssm_parameter_prefix must start with / and have no trailing slash."
  }
}

variable "subnet_id" {
  description = "Subnet to launch the instance in (public, for the EIP)"
  type        = string
}

variable "tags" {
  description = "Tags applied to all resources"
  type        = map(string)
  default     = {}
}

variable "vpc_id" {
  description = "VPC the security group is created in"
  type        = string
}
