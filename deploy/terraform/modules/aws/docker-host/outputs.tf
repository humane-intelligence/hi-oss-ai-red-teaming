output "eip_allocation_id" {
  description = "Allocation id of the Elastic IP"
  value       = aws_eip.this.id
}

output "instance_id" {
  description = "Id of the EC2 instance"
  value       = aws_instance.this.id
}

output "instance_role_name" {
  description = "Name of the instance IAM role, for attaching environment-specific policies"
  value       = aws_iam_role.this.name
}

output "public_ip" {
  description = "Public (Elastic) IP of the instance"
  value       = aws_eip.this.public_ip
}

output "security_group_id" {
  description = "Id of the instance security group"
  value       = aws_security_group.this.id
}
