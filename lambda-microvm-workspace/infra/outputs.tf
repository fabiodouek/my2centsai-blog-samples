output "region" { value = var.region }
output "cloudfront_domain" { value = aws_cloudfront_distribution.cdn.domain_name }
output "cloudfront_distribution_id" { value = aws_cloudfront_distribution.cdn.id }
output "url" { value = "https://${aws_cloudfront_distribution.cdn.domain_name}/" }
output "spa_bucket" { value = aws_s3_bucket.spa.bucket }
output "ecr_repo_url" { value = aws_ecr_repository.backend.repository_url }
output "user_pool_id" { value = aws_cognito_user_pool.pool.id }
output "client_id" { value = aws_cognito_user_pool_client.spa.id }
# Host name only; the managed login is at https://<cognito_domain>/login
output "cognito_domain" { value = "${aws_cognito_user_pool_domain.login.domain}.auth.${var.region}.amazoncognito.com" }
output "table" { value = aws_dynamodb_table.sandboxes.name }
output "alb_dns" { value = aws_lb.alb.dns_name }
output "dns_log_group" { value = local.p2.query_log_group }
output "api_log_group" { value = aws_cloudwatch_log_group.api.name }
output "ecs_cluster" { value = local.p2.ecs_cluster_arn }
output "ecs_service" { value = aws_ecs_service.api.name }
output "image_arn" { value = local.image_arn }
output "api_task_role_arn" { value = aws_iam_role.task.arn }
output "origin_secret" {
  value     = random_password.origin_secret.result
  sensitive = true
}
output "changes_table" { value = aws_dynamodb_table.changes.name }
output "admin_group" { value = aws_cognito_user_group.admins.name }
output "control_plane" { value = local.control_on }
