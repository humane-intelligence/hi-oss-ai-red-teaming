# Nothing downstream reads these automatically: `deploy_role_arn` and `instance_id` are
# copied by hand into the DEPLOY_ROLE_ARN / INSTANCE_ID repository variables that cd.yml
# reads, and the exports bucket name lives in an SSM parameter. So a replacement or
# rename has to be carried over by hand — apply stays green and the mismatch surfaces at
# the next deploy (send-command against a dead id, AccessDenied, or an export writing to
# nowhere). Wiring is in ../../README.md, "Wire CD".

output "deploy_role_arn" {
  description = "ARN of the GitHub Actions OIDC deploy role"
  value       = aws_iam_role.gha_deploy.arn
}

output "exports_bucket" {
  description = "Name of the async-exports S3 bucket"
  value       = aws_s3_bucket.exports.bucket
}

output "instance_id" {
  description = "Id of the dev box EC2 instance"
  value       = module.docker_host.instance_id
}

output "public_ip" {
  description = "Elastic IP of the dev box"
  value       = module.docker_host.public_ip
}
