## Infra

Terraform for the workspace, layered on part 2 (read through `terraform_remote_state`, path in
`var.part2_state_path`). Part 2 must be applied first; nothing here changes its resources.

What it adds:

- A second public subnet (`10.50.1.0/24`, part 2's second AZ) on part 2's public route table, for the ALB.
- DynamoDB `my2cents-ws-sandboxes` (pk `sub`, sk `id`, on-demand, optional `ttl` attribute), and
  `my2cents-ws-policy-changes` for the proxy policy change log.
- Cognito user pool (email sign-in, admin-created users only), a public PKCE client (code flow,
  `openid email`), a managed login domain `my2cents-ws-<random>`, and the group `admins` (may change the
  proxy policy).
- ECR `my2cents-ws-backend`.
- An internet-facing ALB that only CloudFront can reach (SG on the CloudFront origin-facing prefix
  list) and only forwards requests carrying `X-Origin-Verify: <secret>`; anything else gets 403.
  Idle timeout 3600 s for terminal WebSockets.
- ECS Fargate service `workspace-api` (ARM64, 0.5 vCPU / 1 GB) on part 2's cluster and private
  subnets. The origin secret reaches the task from an SSM SecureString, not plain env.
- A private S3 bucket for the SPA and a CloudFront distribution: S3 via OAC by default,
  `/api/*` and `/ws/*` to the ALB uncached with all viewer headers. Client-side routes fall back
  to `index.html` through a CloudFront Function on the S3 behavior only, so API 403/404 responses
  pass through untouched.

Backend task role: MicroVM actions (`lambda:RunMicrovm`, `GetMicrovm`, `SuspendMicrovm`,
`ResumeMicrovm`, `TerminateMicrovm`, `CreateMicrovmAuthToken`, `CreateMicrovmShellAuthToken`,
`GetMicrovmImage`) scoped to the `my2cents-ws-sandbox` image ARN, which is the resource these
actions authorize against; `lambda:ListMicrovms` on `*` (no resource type);
`lambda:PassNetworkConnector` on part 2's connector and the managed ingress connectors; the table;
Live Tail and FilterLogEvents on the proxy and DNS log groups; resource-policy calls on the three
part 2 secrets (never GetSecretValue); the change log table. When part 2 runs with `control_plane=true`
(read from its outputs), also: get/put on the policy objects in its control bucket and get on `status.json`,
and create/put/delete/restore plus resource-policy calls on `my2cents-ip/user/*` secrets (again never
GetSecretValue).

Deploy and users:

```bash
scripts/deploy.sh                          # infra + backend image + SPA, prints the URL
SKIP_BACKEND=1 scripts/deploy.sh           # SPA only
scripts/create_user.sh you@example.com     # prompts for a password; no email is sent
scripts/create_user.sh --admin you@example.com   # same, and adds the user to the admins group
```

`desired_count` defaults to 0 so the stack can exist before an image does; `deploy.sh` applies with
`desired_count=1` and a fresh image tag. A bare `terraform apply` scales the service back to 0.

Cost while deployed (us-east-1): ALB about $0.0225/h plus LCUs, its two public IPv4 addresses
$0.01/h, the Fargate task about $0.02/h. CloudFront, S3, DynamoDB, Cognito (Essentials free tier),
ECR and the log groups are usage-based and round to cents for a demo.

Teardown: `terraform destroy` here, before destroying part 2.
