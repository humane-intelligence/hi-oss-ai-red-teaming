# deploy/terraform

Infrastructure as code for deployment environments. Generic building blocks live in `modules/`, concrete environments in `envs/` — one directory per environment, each a standalone Terraform root module.

```
modules/
  aws/
    docker-host/   # generic VM + network + IAM for a docker-compose stack
envs/
  dev/             # a single-box environment, parameterised — copy it per environment
```

## Provider-agnostic contract

Terraform resources are inherently per-provider, so agnosticism here means: module **interfaces** (inputs/outputs) stay provider-neutral, implementations live in per-provider directories (`modules/aws/...`). AWS is the first implementation; a non-AWS deployment implements the same contract under `modules/<provider>/`. A fully provider-agnostic runtime layer (Kubernetes/Helm) is out of scope for this tree.

## Terraform / OpenTofu

The HCL sticks to the common core of Terraform (BUSL-1.1) and OpenTofu (MPL-2.0): `required_version = ">= 1.10"` holds for both, and both support native S3 state locking from that version. Deployments preferring a fully open-source toolchain can run `tofu` as a drop-in replacement.

The floor is the OpenTofu one: Terraform shipped `use_lockfile` as experimental in 1.10 and promoted it to GA in 1.11 (deprecating the DynamoDB arguments), so a Terraform user on the floor version runs an experimental locking path — CI and the container below pin 1.14.9, well past both.

## State backend

State lives in S3 with native lockfile locking (`use_lockfile = true`) — no DynamoDB table. The bucket and region are supplied at init time through a [partial configuration](https://developer.hashicorp.com/terraform/language/backend#partial-configuration) rather than committed, because bucket names are globally unique and backend blocks take no expressions: copy `backend.hcl.example` to `backend.hcl` (gitignored) and run `terraform init -backend-config=backend.hcl`.

The state bucket itself is deliberately not managed by Terraform (chicken-and-egg); bootstrap it once with the CLI:

```sh
aws s3api create-bucket --bucket <state-bucket>
aws s3api put-bucket-versioning --bucket <state-bucket> \
  --versioning-configuration Status=Enabled
aws s3api put-public-access-block --bucket <state-bucket> \
  --public-access-block-configuration \
  BlockPublicAcls=true,IgnorePublicAcls=true,BlockPublicPolicy=true,RestrictPublicBuckets=true
```

(`create-bucket` as written works only in `us-east-1`; any other region additionally needs `--create-bucket-configuration LocationConstraint=<region>`.)

## Secrets boundary

**No secret material in state, whatever its source** — every managed attribute is stored in plaintext, and the state file lives in S3. In this tree that means:

- SSM SecureString parameters under the app prefix are managed out-of-band with the CLI and are **neither declared nor read** by Terraform. Terraform manages IAM *access* to the parameters (by prefix), never the parameters themselves.
- IAM access keys (here: the SMTP user's) are created and rotated out-of-band; `aws_iam_access_key` would keep the secret in state.
- The SES identity is declared without `dkim_signing_attributes` — adopting BYODKIM means handing Terraform `domain_signing_private_key`, i.e. an RSA private key in state. Easy DKIM (AWS-managed keys) stays the default; a BYODKIM setup belongs out-of-band with the rest.

## Workflow

No host toolchain required — everything runs in a container, on the same patch version CI pins (`terraform-check` in `.github/workflows/ci.yml`), so a local `fmt` verdict matches the gate. Formatting is offline; `validate` needs the network once (`init -backend=false` installs the pinned provider) but no AWS credentials:

```sh
cd deploy/terraform
docker run --rm -v "$PWD:/tf" -w /tf hashicorp/terraform:1.14.9 fmt -recursive
docker run --rm -v "$PWD:/tf" -w /tf/envs/dev --entrypoint sh hashicorp/terraform:1.14.9 \
  -c "terraform init -backend=false -input=false && terraform validate"
```

`init`/`plan`/`apply` against the real backend run from the env directory and need AWS credentials; see the env's own files for backend and provider settings.

## Import runbook (codifying existing infrastructure)

1. Declare the resource in HCL.
2. Add an `import` block in the env — conventionally its own `imports.tf` (`to` = resource address, `id` = live resource id). No environment here ships one; the dev box that was originally adopted this way has been retired.
3. Run `terraform plan` and iterate on the HCL until the plan is clean (`No changes.`). Justified `lifecycle.ignore_changes` entries are documented with a one-line why next to the resource.
4. Import blocks stay in the repo as a record of adoption; once the resource is in state they are no-ops — but only while their ids resolve. They are evaluated for every address missing from state, so after a replacement (or a lost state) a stale id fails the *whole* plan; drop or update the block then. The ids also belong to one environment: a second environment adopts nothing by copying the first one's blocks, and resources that exist once per scope — the OIDC provider per account, the SES identity per account and region — can only be adopted by one of them.

The `docker-host` instance and its EIP carry `lifecycle.prevent_destroy`: the root volume is the only copy of whatever the host stores, and the address is what DNS points at. `lifecycle` blocks take literals, so a consumer cannot opt out from the outside — a deliberate teardown means dropping the guard in the module and applying that.

## Standing up a new environment (bring your own AWS)

`envs/dev` is a parameterised environment, not a description of someone else's account: every value that ties it to one account, region, repository or domain is a variable. Applying it on a fresh AWS account means supplying those variables, not editing HCL.

The phases below are derived from how the original box was built, but nobody has re-walked them end to end on a scratch account — treat them as a map, not a script, and fix what you find.

### 1. Your own env directory

```sh
cp -r envs/dev envs/<name>
rm -rf envs/<name>/.terraform      # copied init: would offer to migrate the source env's state
cd envs/<name>
cp terraform.tfvars.example terraform.tfvars
cp backend.hcl.example backend.hcl
```

Both copies are gitignored. Fill in `terraform.tfvars` — four values have no default and must be yours:

| Variable | What it is |
|---|---|
| `aws_account_id` | The account to apply into. `providers.tf` refuses to apply anywhere else, so a stray `AWS_PROFILE` fails fast instead of building your infrastructure in the wrong place |
| `vpc_id` | The VPC for the security group |
| `subnet_id` | A **public** subnet — the module attaches an Elastic IP |
| `mail_domain` | Registered as an SES email identity; see [SES](#ses-email) |

Everything else has a default worth knowing about:

- `name_prefix` (`ai-red-teaming-dev`) prefixes every resource name, including the ones the module does not reach — the deploy role, the SMTP user, the instance role's inline policies and the exports bucket. IAM role and user names are **account-global**, so two environments in one account need different prefixes or the second fails on `EntityAlreadyExists`. A distinct prefix is necessary but not sufficient: `aws_iam_openid_connect_provider.github` is unprefixed and an account allows only one provider per URL, so a second environment in the same account must import the existing one rather than create it. The exports bucket appends your account id, since S3 names are global across all accounts.
- `github_repo` (`humane-intelligence/hi-oss-ai-red-teaming`) is the only repository allowed to assume the CD deploy role, and only from `refs/heads/main`. **Point this at your own fork** or CD cannot deploy. Note that an AWS account allows exactly one OIDC provider per URL: if `token.actions.githubusercontent.com` already exists in yours, import it rather than letting this create a second.
- `ami_id` is `null`, which resolves the newest Ubuntu 24.04 at plan time. Pin one only for reproducible rebuilds, and only for your own region — AMI ids are region-scoped, so a foreign id fails the first plan with `InvalidAMIID.NotFound`.
- `aws_region`, `instance_type`, `ssm_parameter_prefix` and `tags` are ordinary defaults; `variables.tf` documents each.

Then `init` with your backend config and `apply`, from that directory and with AWS credentials — in a container if you have no host toolchain, as in [Workflow](#workflow) above:

```sh
terraform init -backend-config=backend.hcl
terraform apply
```

CI's `Terraform check` picks the new directory up automatically (it validates every `envs/*/`, and `init -backend=false` needs neither your backend config nor your variables). Tearing the environment down later runs into the `prevent_destroy` guards described above.

### 2. Secrets and DNS

- Create the parameters under your `ssm_parameter_prefix` — the required set is the table in [`../README.md`](../README.md#prerequisites-one-time). SecureStrings are created with the CLI; Terraform manages *access* to the prefix, never the values.
- Point `APP_DOMAIN` at the instance's Elastic IP (an A record) **before** the first deploy. Caddy provisions the TLS certificate over an HTTP-01 challenge, so without working DNS and a reachable port 80 the site never comes up on HTTPS. An `<anything>-<ip>.sslip.io` name works for a quick start.

### 3. Bootstrap the box

The module doesn't manage `user_data`, so a freshly applied instance is bare Ubuntu with an SSM agent. Once, through Session Manager (`start-session` needs the [Session Manager plugin](https://docs.aws.amazon.com/systems-manager/latest/userguide/session-manager-working-with-install-plugin.html) next to the AWS CLI):

```sh
aws ssm start-session --target <instance-id> --region <region>
sudo -i
```

1. Run the contents of [`../ec2-bootstrap.sh`](../ec2-bootstrap.sh) (installs Docker + compose, the AWS CLI, git; creates `/opt/aibackend`).
2. Put this repo at `/opt/aibackend` — a sparse checkout of `deploy/` and `monitoring/` over a read-only deploy key is enough (CD checks out both paths). CD keeps it in lockstep with the deployed commit from then on.

> Once the module takes a `user_data` input this phase disappears — a new instance bootstraps itself on create. Until then the module carries `ignore_changes = [ami, user_data]` for every consumer, not just the box this was first written for. The two halves guard different failure modes: an `ami` change forces **replacement**, so the guard means your instance stays on whatever AMI it launched with (the newest pattern match, unless you pinned `ami_id`) and moving it later is a deliberate, replacing change; unmanaged `user_data` drift would instead be **cleared in place** — the provider applies it through a stop → modify → start cycle, wiping the bootstrap record this module doesn't manage.

### 4. Wire CD

- **Repository variables** (Settings > Secrets and variables > Actions > Variables) — `DEPLOY_ROLE_ARN` (this env's `deploy_role_arn` output), `INSTANCE_ID` (its `instance_id` output), and optionally `AWS_REGION` and `DEPLOY_DIR`. The workflow's `env:` block reads them; the deploy jobs skip themselves when `DEPLOY_ROLE_ARN` is unset, so a fork with no infrastructure still publishes images instead of failing. Image names follow the repository and need no configuration.
- GHCR — after the first publish, link each package to the repo and set it Private, then create a `read:packages` PAT and store it as `GHCR_USER` / `GHCR_PAT` in Parameter Store.
- The full permission picture (what CD, the box, and you each need) is in [`../PERMISSIONS.md`](../PERMISSIONS.md).

### 5. First deploy and first admin

Merge to `main` — or, for an environment that isn't fed by CD yet, use the manual path in [`../README.md`](../README.md#manual-deploy-unmerged-branch-or-debugging). Verify with the `curl` checks there, then create the first admin by hand (that README's "First admin" section) — there is no automatic seed.

### SES (email)

The env manages an SES email identity and an IAM user for SMTP, but two steps sit outside Terraform by nature:

- **Verify the identity** — publish the DNS records AWS gives you for the domain; nothing sends until it verifies.
- **Leave the SES sandbox** — a support case. In the sandbox, SES only delivers to verified addresses, which is enough for a smoke test and not enough for real invitations.

The SMTP password is a SigV4 derivative of the IAM user's access key. The key is deliberately not managed by Terraform (secret material) — create and rotate it out-of-band.
