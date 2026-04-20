# AWS Security Agent Demo

Companion code for the [AWS Security Agent: Automated Pentesting, Code Reviews, and Design Reviews](https://my2centsai.com/deep-dive/aws-security-agent/) deep dive on [my2cents.ai](https://my2centsai.com).

This repo contains a vendored copy of [INE's AWSGoat](https://github.com/ine-labs/AWSGoat) — a vulnerable-by-design AWS infrastructure — plus the Terraform changes referenced in the blog post. It's intended as a ready-to-deploy target for AWS Security Agent, so you can reproduce the pentest walkthrough end-to-end.

The original AWSGoat README is preserved unchanged as [README-original.md](README-original.md).

## What the blog post covers

The post walks through using **AWS Security Agent** to pentest AWSGoat, including:

- Deploying AWSGoat's two modules (Module 1: Lambda/API Gateway/DynamoDB blog; Module 2: ECS/ALB/RDS HR payroll app)
- Standing up a custom HTTPS-terminated domain in front of each module so the agent has a stable, TLS-validated target
- Configuring an Agent Space, verifying domain ownership, and providing credentials via Secrets Manager
- Reviewing the agent's findings against the documented OWASP-style vulnerabilities in AWSGoat
- Fixes for the gotchas encountered along the way (Apple Silicon provider mismatches, BSD `sed`, RDS seeding, dump.sql duplicate PKs)

All of that is in the post: **[AWS Security Agent deep dive](https://my2centsai.com/deep-dive/aws-security-agent/)**.

## What's different from upstream AWSGoat

Four intentional changes are baked in. All of them are covered in the [blog post](https://my2centsai.com/deep-dive/aws-security-agent/); the summary below is the quick reference.

### 1. Optional custom domain for Module 1 (API Gateway)

`modules/module-1/main.tf` adds two optional variables — `route53_zone_name` and `custom_domain_name`. When set, Terraform provisions an ACM public certificate, DNS validation records in the named Route 53 hosted zone, an `aws_api_gateway_domain_name`, a base-path mapping, and an alias A record pointing the custom name at the API Gateway edge endpoint.

```bash
cd modules/module-1
terraform apply \
  -var route53_zone_name=example.com \
  -var custom_domain_name=m1.example.com
```

Output: `custom_app_url = "https://m1.example.com/react"`.

### 2. Optional custom domain for Module 2 (ALB)

`modules/module-2/main.tf` adds the same two variables. When set, Terraform provisions an ACM public certificate, DNS validation records, an `aws_lb_listener` on 443 that forwards to the existing Module 2 target group, a security-group ingress rule for 443, and an alias A record pointing the custom name at the ALB.

```bash
cd modules/module-2
terraform apply \
  -var route53_zone_name=example.com \
  -var custom_domain_name=m2.example.com
```

Output: `custom_app_url = "https://m2.example.com/login.php"`.

Both blocks are `count`-guarded: if you don't set the variables, the modules deploy exactly as upstream and the existing `app_url` / `ad_Target_URL` outputs (raw AWS URLs) are unchanged.

### 3. Module 2 DB seed helper (`scripts/seed-module-2.sh`)

The Module 2 ECS task definition pulls the upstream prebuilt public image (`public.ecr.aws/p3q0v3y2/aws-goat-m2:latest`), which does **not** seed RDS on its own. After `terraform apply` finishes, the login page will load but every query fails until the DB is populated.

Run the helper script once after apply:

```bash
scripts/seed-module-2.sh
```

What it does:

- Finds the ECS EC2 instance via the `ecs-lab-cluster` cluster's registered container instance,
- Fetches the DB username and password from Secrets Manager (`RDS_CREDS`) — no credentials are baked into the script,
- Reads the RDS endpoint from `aws-goat-db`,
- Base64-encodes the repo-local `modules/module-2/src/src/dump.sql` (pre-pending `DROP DATABASE IF EXISTS appdb;` and appending `COMMIT;` so the script is idempotent),
- Ships the payload to the EC2 host via `aws ssm send-command`, which `docker cp`s it into the running container and runs `mysql < /tmp/dump.sql`,
- Polls SSM and prints the final status.

Only the AWS CLI and `base64` are required on your machine — no Docker, no MySQL client, no SSH. The script is safe to re-run: the `DROP DATABASE IF EXISTS appdb` prefix resets state before loading.

**Customizing**: every hardcoded identifier in the script can be overridden by env var, so forks that rename resources don't need to edit the file:

| Env var | Default | Source |
|---|---|---|
| `AWS_REGION` | `us-east-1` | Module 2's provider |
| `CLUSTER_NAME` | `ecs-lab-cluster` | `main.tf` (ECS cluster) |
| `DB_IDENTIFIER` | `aws-goat-db` | `main.tf` (RDS instance) |
| `CONTAINER_LABEL` | `aws-goat-m2` | `resources/ecs/task_definition.json` |
| `SECRET_ID` | `RDS_CREDS` | `main.tf` (Secrets Manager secret) |
| `DUMP_FILE` | `modules/module-2/src/src/dump.sql` | this repo |

An auto-seed version of `modules/module-2/src/script/startup.sh` ships in the repo for anyone who forks and builds their own container image (in which case the seed happens at container start and the helper script becomes unnecessary). With the default deploy path — the public image — that startup script never runs; the helper script is what you need.

### 4. Module 2 `dump.sql` duplicate primary key fix

`modules/module-2/src/src/dump.sql` had two rows in the `reimbursement` table that reused primary keys already present earlier in the file. Under MySQL's default stop-on-error behaviour and the `AUTOCOMMIT=0` / missing-`COMMIT` pattern in the dump, the collisions aborted the load mid-way and left every table empty. Two `reimbursment_id` values are changed (`'3'` → `'5'`, `'4'` → `'6'`) so the dump loads cleanly. The seed helper above uses this fixed dump.

### Upstream fixes already applied

Two upstream-reported fixes are baked in so you don't have to patch anything on a Mac:

- **Apple Silicon / Terraform 1.x**: the deprecated `template_file` data source is replaced with `templatefile()`, `file()`, or `local_file` where it appeared in both modules.
- **BSD `sed`**: every `sed -i` host-side provisioner uses `sed -i.bak '...'` so the same command works on macOS and Linux.

## Prerequisites

- An AWS account you own (AWSGoat is intentionally vulnerable — **never deploy to a shared or production account**)
- AWS CLI configured with admin-ish credentials (or the policy in [policy/policy.json](policy/policy.json))
- Terraform installed locally
- For the custom domain pieces: an existing public Route 53 hosted zone in the same account
- AWS Security Agent enabled in a [supported region](https://console.aws.amazon.com/securityagent/) (the post uses `us-east-1`)

## Before `terraform apply`: patch the placeholder account ID

Three files under `modules/module-1/resources/` ship with a placeholder AWS account ID `111111111111` baked into S3 bucket names and URLs, so the repo is safe to read publicly. Before deploying Module 1, replace the placeholder with your own 12-digit account ID so the string baked into Lambda/DynamoDB matches the bucket names Terraform actually creates:

```bash
cd aws-security-agent-demo
MY_ACCOUNT_ID=$(aws sts get-caller-identity --query Account --output text)
find modules/module-1/resources \
  \( -name lambda_function.py -o -name index.js -o -name blog-posts.json \) \
  -exec sed -i.bak "s/111111111111/${MY_ACCOUNT_ID}/g" {} +
```

The `.bak` files `sed` leaves behind can be deleted after the replacement. Module 2 does not need this step.

## Quick start

1. Clone this repo.
2. **Module 1 only**: run the `sed` replacement above so bucket names match the deployer's account ID.
3. Deploy one or both modules per the commands above (or follow the upstream instructions in [README-original.md](README-original.md) to skip the custom domain).
4. **Module 2 only**: run `scripts/seed-module-2.sh` after `terraform apply` finishes to populate the RDS `appdb` database.
5. Follow the [blog post](https://my2centsai.com/deep-dive/aws-security-agent/) to wire the resulting URL into an Agent Space and start a pentest run.
6. When done, `terraform destroy` in each module directory.

## Cost

- **AWSGoat**: Module 1 ≈ $0.0125/hour, Module 2 ≈ $0.0505/hour (upstream estimate; mostly free-tier-friendly).
- **Custom domain additions**: ACM public certs are free; Route 53 hosted zone is $0.50/month per zone.
- **Security Agent**: $50/task-hour at the time of writing. See the blog post for a real-world runtime breakdown.

## Warning

AWSGoat is **intentionally vulnerable** and exposes a public HTTP/HTTPS surface once deployed. Use a dedicated sandbox AWS account, destroy the stacks when you're finished, and don't point your custom domain at AWSGoat from a zone that also serves production traffic.

### Private keys in the repo

Module 1 vendors a directory of RSA private keys at `modules/module-1/resources/s3/shared/shared/files/.ssh/keys/*.pem` (alice, bob, charles, john, mary, mike, sophia, VincentVanGoat). These mirror upstream AWSGoat, where they are an **intentional part of the vulnerability path** — the CTF-style attack chain depends on discovering and using them from a misconfigured S3 bucket. They are not secret: the same keys are checked into the public INE-labs repo.

Because they are public and shared across every deploy of AWSGoat, treat them as **demo-only**:

- Never reuse these keypairs for any real EC2 instance outside the sandbox.
- The sandbox account warning above exists specifically so that a compromise via one of these keys cannot cross into anything you care about.
- `terraform destroy` after the walkthrough to take the EC2 hosts they authorize offline.

## License

The upstream AWSGoat code is MIT-licensed — see [LICENSE](LICENSE). The additions in this repo are published under the same terms.

## Credits

- AWSGoat by [INE](https://ine.com/) — [github.com/ine-labs/AWSGoat](https://github.com/ine-labs/AWSGoat)
- Blog post and vendored modifications by [my2cents.ai](https://my2centsai.com)
