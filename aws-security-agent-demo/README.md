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

This vendored copy adds two optional variables per module so the API Gateway (Module 1) and ALB (Module 2) can be fronted by a custom Route 53 DNS name with an ACM-issued TLS certificate:

```bash
cd modules/module-1
terraform apply \
  -var route53_zone_name=example.com \
  -var custom_domain_name=m1.example.com

cd ../module-2
terraform apply \
  -var route53_zone_name=example.com \
  -var custom_domain_name=m2.example.com
```

If you don't set the variables, the modules deploy exactly as upstream — the new resources are count-guarded and default to disabled.

Outputs:

- Module 1: `custom_app_url = "https://m1.example.com/react"`
- Module 2: `custom_app_url = "https://m2.example.com/login.php"`

The existing `app_url` / `ad_Target_URL` outputs (raw AWS URLs) are unchanged.

## Prerequisites

- An AWS account you own (AWSGoat is intentionally vulnerable — **never deploy to a shared or production account**)
- AWS CLI configured with admin-ish credentials (or the policy in [policy/policy.json](policy/policy.json))
- Terraform installed locally
- For the custom domain pieces: an existing public Route 53 hosted zone in the same account
- AWS Security Agent enabled in a [supported region](https://console.aws.amazon.com/securityagent/) (the post uses `us-east-1`)

## Quick start

1. Clone this repo.
2. Deploy one or both modules per the commands above (or follow the upstream instructions in [README-original.md](README-original.md) to skip the custom domain).
3. Follow the [blog post](https://my2centsai.com/deep-dive/aws-security-agent/) to wire the resulting URL into an Agent Space and start a pentest run.
4. When done, `terraform destroy` in each module directory.

## Cost

- **AWSGoat**: Module 1 ≈ $0.0125/hour, Module 2 ≈ $0.0505/hour (upstream estimate; mostly free-tier-friendly).
- **Custom domain additions**: ACM public certs are free; Route 53 hosted zone is $0.50/month per zone.
- **Security Agent**: $50/task-hour at the time of writing. See the blog post for a real-world runtime breakdown.

## Warning

AWSGoat is **intentionally vulnerable** and exposes a public HTTP/HTTPS surface once deployed. Use a dedicated sandbox AWS account, destroy the stacks when you're finished, and don't point your custom domain at AWSGoat from a zone that also serves production traffic.

## License

The upstream AWSGoat code is MIT-licensed — see [LICENSE](LICENSE). The additions in this repo are published under the same terms.

## Credits

- AWSGoat by [INE](https://ine.com/) — [github.com/ine-labs/AWSGoat](https://github.com/ine-labs/AWSGoat)
- Blog post and vendored modifications by [my2cents.ai](https://my2centsai.com)
