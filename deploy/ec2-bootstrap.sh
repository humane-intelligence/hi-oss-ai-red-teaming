#!/usr/bin/env bash
# EC2 user-data: install Docker + compose plugin, the AWS CLI, and
# unattended-upgrades, create the deploy dir. App ships as an image, so no Python
# toolchain here. The SSM agent is preinstalled on the Ubuntu 24.04 AMI (else:
# snap install amazon-ssm-agent).
set -euxo pipefail

export DEBIAN_FRONTEND=noninteractive
apt-get update
apt-get install -y ca-certificates curl git unattended-upgrades

# AWS CLI for deploy.sh (reads SSM Parameter Store). Ubuntu has no aws CLI by
# default; the snap is v2 with classic confinement (full filesystem access).
snap install aws-cli --classic

install -m 0755 -d /etc/apt/keyrings
curl -fsSL https://download.docker.com/linux/ubuntu/gpg -o /etc/apt/keyrings/docker.asc
chmod a+r /etc/apt/keyrings/docker.asc
echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc] \
https://download.docker.com/linux/ubuntu $(. /etc/os-release && echo "$VERSION_CODENAME") stable" \
	>/etc/apt/sources.list.d/docker.list
apt-get update
apt-get install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
systemctl enable --now docker

mkdir -p /opt/aibackend
