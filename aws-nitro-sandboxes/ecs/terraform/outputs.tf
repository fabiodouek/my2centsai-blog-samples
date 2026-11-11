output "vpc_id" {
  value = module.vpc.vpc_id
}

output "private_subnets" {
  value = module.vpc.private_subnets
}

output "security_group" {
  value = aws_security_group.tasks.id
}

output "infrastructure_role_arn" {
  value = aws_iam_role.infrastructure.arn
}

output "instance_profile_arn" {
  value = aws_iam_instance_profile.instance.arn
}

output "execution_role_arn" {
  value = aws_iam_role.execution.arn
}

output "task_role_arn" {
  value = aws_iam_role.task.arn
}

output "log_group" {
  value = aws_cloudwatch_log_group.tasks.name
}

# The value for `aws ecs run-task --network-configuration`.
output "run_task_network_configuration" {
  value = "awsvpcConfiguration={subnets=[${join(",", module.vpc.private_subnets)}],securityGroups=[${aws_security_group.tasks.id}]}"
}
