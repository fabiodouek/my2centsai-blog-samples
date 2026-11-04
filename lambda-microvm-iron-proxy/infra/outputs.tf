output "region" { value = var.region }
output "strict" { value = var.strict }
output "azs" { value = aws_subnet.proxy[*].availability_zone }
output "az_ids" { value = var.az_ids }

# ---------- What the scripts use ----------
output "nlb_name" { value = aws_lb.nlb.name }
output "nlb_dns" { value = aws_lb.nlb.dns_name }
output "nlb_ips" { value = local.nlb_ips }
output "nat_public_ip" { value = aws_eip.nat.public_ip }
output "nat_private_ip" { value = aws_nat_gateway.nat.private_ip }
output "connector_arn" { value = aws_lambdacore_network_connector.sandbox.arn }
output "artifacts_bucket" { value = aws_s3_bucket.artifacts.bucket }
output "demo_bucket" { value = aws_s3_bucket.demo.bucket }
output "build_role_arn" { value = aws_iam_role.build.arn }
output "exec_logs_role_arn" { value = aws_iam_role.exec_logs.arn }
output "ecs_service" { value = aws_ecs_service.proxy.name }
output "proxy_log_group" { value = aws_cloudwatch_log_group.proxy.name }
output "ca_cert_pem" { value = tls_self_signed_cert.ca.cert_pem }
output "placeholders" { value = local.placeholders }
output "secret_arns" { value = { for k, s in aws_secretsmanager_secret.injected : k => s.arn } }

# ---------- The sandbox VPC (part 3's sandboxes run in it too) ----------
output "sandbox_vpc_id" { value = aws_vpc.sandbox.id }
output "sandbox_vpc_cidr" { value = aws_vpc.sandbox.cidr_block }
output "connector_subnet_ids" { value = aws_subnet.connector[*].id }
output "connector_subnet_cidrs" { value = aws_subnet.connector[*].cidr_block }
output "connector_sg_id" { value = aws_security_group.connector.id }
output "sandbox_route_table_id" { value = aws_route_table.sandbox.id }
output "peering_id" { value = aws_vpc_peering_connection.p.id }
output "operator_role_arn" { value = aws_iam_role.operator.arn }
output "sandbox_zone_id" { value = aws_route53_zone.sandbox.zone_id }
output "proxy_name" { value = aws_route53_record.proxy.fqdn }
output "firewall_rule_group_id" { value = aws_route53_resolver_firewall_rule_group.g.id }
output "firewall_association_id" { value = aws_route53_resolver_firewall_rule_group_association.sandbox.id }
output "query_log_config_id" { value = aws_route53_resolver_query_log_config.sandbox.id }
output "query_log_group" { value = aws_cloudwatch_log_group.queries.name }
output "alarm_log_group" { value = aws_cloudwatch_log_group.alarm.name }
output "alarm_topic_arn" { value = aws_sns_topic.alarm.arn }
output "alarm_rule_name" { value = aws_cloudwatch_event_rule.alarm.name }

# ---------- The proxy VPC and iron-proxy, for part 3 (the workspace), which builds on them ----------
output "proxy_vpc_id" { value = aws_vpc.proxy.id }
output "proxy_vpc_cidr" { value = aws_vpc.proxy.cidr_block }
output "igw_id" { value = aws_internet_gateway.igw.id }
output "public_subnet_id" { value = aws_subnet.public.id }
output "public_route_table_id" { value = aws_route_table.public.id }
output "proxy_subnet_ids" { value = aws_subnet.proxy[*].id }
output "proxy_subnet_cidrs" { value = aws_subnet.proxy[*].cidr_block }
output "nlb_reservation_cidrs" { value = aws_ec2_subnet_cidr_reservation.nlb[*].cidr_block }
output "proxy_route_table_id" { value = aws_route_table.proxy.id }
output "nat_gateway_id" { value = aws_nat_gateway.nat.id }
output "nlb_arn" { value = aws_lb.nlb.arn }
output "nlb_sg_id" { value = aws_security_group.nlb.id }
output "proxy_sg_id" { value = aws_security_group.proxy.id }
output "ecs_cluster" { value = aws_ecs_cluster.c.name }
output "ecs_cluster_arn" { value = aws_ecs_cluster.c.arn }
output "iron_proxy_image" { value = var.iron_proxy_image }
output "task_role_arn" { value = aws_iam_role.task.arn }
output "task_role_name" { value = aws_iam_role.task.name }
output "task_exec_role_arn" { value = aws_iam_role.task_exec.arn }
output "task_exec_role_name" { value = aws_iam_role.task_exec.name }
output "ca_key_secret_arn" { value = aws_secretsmanager_secret.ca_key.arn }

# ---------- Part 3's proxy control plane (control_plane=true; null otherwise) ----------
output "control_plane" { value = var.control_plane }
output "control_bucket" { value = one(aws_s3_bucket.control[*].bucket) }
output "control_bucket_arn" { value = one(aws_s3_bucket.control[*].arn) }
output "control_keys" { value = var.control_plane ? local.control_keys : null }
output "sandbox_roles" {
  value = var.control_plane ? { for k, r in aws_iam_role.sandbox : k => {
    arn = r.arn, name = r.name, description = r.description, default = local.sandbox_roles[k].default
  } } : null
}
output "default_role" { value = var.control_plane ? local.default_role : null }
output "scope_baseline" { value = var.control_plane ? local.scope_baseline : null }
output "user_secret_prefix" { value = var.control_plane ? var.user_secret_prefix : null }
output "mgmt_secret_arn" { value = one(aws_secretsmanager_secret.mgmt[*].arn) }
output "demo_table" { value = one(aws_dynamodb_table.demo[*].name) }
