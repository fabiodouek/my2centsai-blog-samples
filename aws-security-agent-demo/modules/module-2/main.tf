terraform {
  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 3.27"
    }
  }
}

provider "aws" {
  region = "us-east-1"
}

variable "route53_zone_name" {
  description = "Name of an existing Route 53 public hosted zone (e.g. example.com). Leave empty to skip custom domain setup."
  type        = string
  default     = ""
}

variable "custom_domain_name" {
  description = "FQDN for Module 2's ALB custom domain (e.g. m2.example.com). Must be a subdomain of var.route53_zone_name. Required if route53_zone_name is set."
  type        = string
  default     = ""
}

locals {
  create_custom_domain = var.route53_zone_name != "" && var.custom_domain_name != ""
}

data "aws_route53_zone" "custom" {
  count        = local.create_custom_domain ? 1 : 0
  name         = var.route53_zone_name
  private_zone = false
}

data "aws_caller_identity" "current" {}

data "aws_availability_zones" "available" {
  state = "available"
}

# VPC Config for public access
resource "aws_vpc" "lab-vpc" {
  cidr_block           = "10.0.0.0/16"
  enable_dns_support   = true
  enable_dns_hostnames = true
  tags = {
    Name = "AWS_GOAT_VPC"
  }
}
resource "aws_subnet" "lab-subnet-public-1" {
  vpc_id                  = aws_vpc.lab-vpc.id
  cidr_block              = "10.0.1.0/24"
  map_public_ip_on_launch = true
  availability_zone       = data.aws_availability_zones.available.names[0]
}
resource "aws_internet_gateway" "my_vpc_igw" {
  vpc_id = aws_vpc.lab-vpc.id
  tags = {
    Name = "My VPC - Internet Gateway"
  }
}
resource "aws_route_table" "my_vpc_us_east_1_public_rt" {
  vpc_id = aws_vpc.lab-vpc.id
  route {
    cidr_block = "0.0.0.0/0"
    gateway_id = aws_internet_gateway.my_vpc_igw.id
  }

  tags = {
    Name = "Public Subnet Route Table."
  }
}

resource "aws_route_table_association" "my_vpc_us_east_1a_public" {
  subnet_id      = aws_subnet.lab-subnet-public-1.id
  route_table_id = aws_route_table.my_vpc_us_east_1_public_rt.id
}
resource "aws_subnet" "lab-subnet-public-1b" {
  vpc_id                  = aws_vpc.lab-vpc.id
  cidr_block              = "10.0.128.0/24"
  availability_zone       = data.aws_availability_zones.available.names[1]
  map_public_ip_on_launch = true
}
resource "aws_route_table_association" "my_vpc_us_east_1b_public" {
  subnet_id      = aws_subnet.lab-subnet-public-1b.id
  route_table_id = aws_route_table.my_vpc_us_east_1_public_rt.id
}

resource "aws_security_group" "ecs_sg" {
  name        = "ECS-SG"
  description = "SG for cluster created from terraform"
  vpc_id      = aws_vpc.lab-vpc.id

  ingress {
    from_port       = 0
    to_port         = 65535
    protocol        = "tcp"
    security_groups = [aws_security_group.load_balancer_security_group.id]
  }

  egress {
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }
}

# Create Database Subnet Group
# terraform aws db subnet group
resource "aws_db_subnet_group" "database-subnet-group" {
  name        = "database subnets"
  subnet_ids  = [aws_subnet.lab-subnet-public-1.id, aws_subnet.lab-subnet-public-1b.id]
  description = "Subnets for Database Instance"

  tags = {
    Name = "Database Subnets"
  }
}

# Create Security Group for the Database
# terraform aws create security group

resource "aws_security_group" "database-security-group" {
  name        = "Database Security Group"
  description = "Enable MYSQL Aurora access on Port 3306"
  vpc_id      = aws_vpc.lab-vpc.id

  ingress {
    description     = "MYSQL/Aurora Access"
    from_port       = 3306
    to_port         = 3306
    protocol        = "tcp"
    security_groups = ["${aws_security_group.ecs_sg.id}"]
  }

  egress {
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }

  tags = {
    Name = "rds-db-sg"
  }

}

# Create Database Instance Restored from DB Snapshots
# terraform aws db instance
resource "aws_db_instance" "database-instance" {
  identifier             = "aws-goat-db"
  allocated_storage      = 10
  instance_class         = "db.t3.micro"
  engine                 = "mysql"
  engine_version         = "8.0"
  username               = "root"
  password               = "T2kVB3zgeN3YbrKS"
  parameter_group_name   = "default.mysql8.0"
  skip_final_snapshot    = true
  availability_zone      = "us-east-1a"
  db_subnet_group_name   = aws_db_subnet_group.database-subnet-group.name
  vpc_security_group_ids = [aws_security_group.database-security-group.id]
}



resource "aws_security_group" "load_balancer_security_group" {
  name        = "Load-Balancer-SG"
  description = "SG for load balancer created from terraform"
  vpc_id      = aws_vpc.lab-vpc.id

  ingress {
    from_port   = 80
    to_port     = 80
    protocol    = "tcp"
    cidr_blocks = ["0.0.0.0/0"]
  }

  egress {
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }
  tags = {
    Name = "aws-goat-m2-sg"
  }
}



resource "aws_iam_role" "ecs-instance-role" {
  name                 = "ecs-instance-role"
  path                 = "/"
  permissions_boundary = aws_iam_policy.instance_boundary_policy.arn
  assume_role_policy = jsonencode({
    "Version" : "2008-10-17",
    "Statement" : [
      {
        "Sid" : "",
        "Effect" : "Allow",
        "Principal" : {
          "Service" : "ec2.amazonaws.com"
        },
        "Action" : "sts:AssumeRole"
      }
    ]
  })
}


resource "aws_iam_role_policy_attachment" "ecs-instance-role-attachment-1" {
  role       = aws_iam_role.ecs-instance-role.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AmazonEC2ContainerServiceforEC2Role"
}
resource "aws_iam_role_policy_attachment" "ecs-instance-role-attachment-2" {
  role       = aws_iam_role.ecs-instance-role.name
  policy_arn = "arn:aws:iam::aws:policy/IAMFullAccess"
}

resource "aws_iam_role_policy_attachment" "ecs-instance-role-attachment-3" {
  role       = aws_iam_role.ecs-instance-role.name
  policy_arn = aws_iam_policy.ecs_instance_policy.arn
}

resource "aws_iam_policy" "ecs_instance_policy" {
  name = "aws-goat-instance-policy"
  policy = jsonencode({
    "Statement" : [
      {
        "Action" : [
          "ssm:*",
          "ssmmessages:*",
          "ec2:RunInstances",
          "ec2:Describe*"
        ],
        "Effect" : "Allow",
        "Resource" : "*",
        "Sid" : "Pol1"
      }
    ],
    "Version" : "2012-10-17"
  })
}

resource "aws_iam_policy" "instance_boundary_policy" {
  name = "aws-goat-instance-boundary-policy"
  policy = jsonencode({
    "Statement" : [
      {
        "Action" : [
          "iam:List*",
          "iam:Get*",
          "iam:PassRole",
          "iam:PutRole*",
          "ssm:*",
          "ssmmessages:*",
          "ec2:RunInstances",
          "ec2:Describe*",
          "ecs:*",
          "ecr:*",
          "logs:CreateLogStream",
          "logs:PutLogEvents"
        ],
        "Effect" : "Allow",
        "Resource" : "*",
        "Sid" : "Pol1"
      }
    ],
    "Version" : "2012-10-17"
  })
}

resource "aws_iam_instance_profile" "ec2-deployer-profile" {
  name = "ec2Deployer"
  path = "/"
  role = aws_iam_role.ec2-deployer-role.id
}
resource "aws_iam_role" "ec2-deployer-role" {
  name = "ec2Deployer-role"
  path = "/"
  assume_role_policy = jsonencode({
    "Version" : "2008-10-17",
    "Statement" : [
      {
        "Sid" : "",
        "Effect" : "Allow",
        "Principal" : {
          "Service" : "ec2.amazonaws.com"
        },
        "Action" : "sts:AssumeRole"
      }
    ]
  })
}

resource "aws_iam_policy" "ec2_deployer_admin_policy" {
  name = "ec2DeployerAdmin-policy"
  policy = jsonencode({
    "Statement" : [
      {
        "Action" : [
          "*"
        ],
        "Effect" : "Allow",
        "Resource" : "*",
        "Sid" : "Policy1"
      }
    ],
    "Version" : "2012-10-17"
  })
}

resource "aws_iam_role_policy_attachment" "ec2-deployer-role-attachment" {
  role       = aws_iam_role.ec2-deployer-role.name
  policy_arn = aws_iam_policy.ec2_deployer_admin_policy.arn
}

resource "aws_iam_instance_profile" "ecs-instance-profile" {
  name = "ecs-instance-profile"
  path = "/"
  role = aws_iam_role.ecs-instance-role.id
}
resource "aws_iam_role" "ecs-task-role" {
  name = "ecs-task-role"
  path = "/"
  assume_role_policy = jsonencode({
    "Version" : "2012-10-17",
    "Statement" : [
      {
        "Sid" : "",
        "Effect" : "Allow",
        "Principal" : {
          "Service" : "ecs-tasks.amazonaws.com"
        },
        "Action" : "sts:AssumeRole"
      }
    ]
    }
  )
}

resource "aws_iam_role_policy_attachment" "ecs-task-role-attachment" {
  role       = aws_iam_role.ecs-task-role.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AmazonECSTaskExecutionRolePolicy"
}
resource "aws_iam_role_policy_attachment" "ecs-task-role-attachment-2" {
  role       = aws_iam_role.ecs-task-role.name
  policy_arn = "arn:aws:iam::aws:policy/SecretsManagerReadWrite"
}

resource "aws_iam_role_policy_attachment" "ecs-instance-role-attachment-ssm" {
  role       = aws_iam_role.ecs-instance-role.name
  policy_arn = "arn:aws:iam::aws:policy/AmazonSSMManagedInstanceCore"
}


data "aws_ami" "ecs_optimized_ami" {
  most_recent = true
  owners      = ["amazon"]

  filter {
    name   = "name"
    values = ["amzn2-ami-ecs-hvm-2.0.202*-x86_64-ebs"]
  }
}


resource "aws_launch_template" "ecs_launch_template" {
  name_prefix   = "ecs-launch-template-"
  image_id      = data.aws_ami.ecs_optimized_ami.id
  instance_type = "t2.micro"

  iam_instance_profile {
    name = aws_iam_instance_profile.ecs-instance-profile.name
  }

  vpc_security_group_ids = [aws_security_group.ecs_sg.id]
  user_data              = base64encode(file("${path.module}/resources/ecs/user_data.tpl"))
}

resource "aws_autoscaling_group" "ecs_asg" {
  name                = "ECS-lab-asg"
  vpc_zone_identifier = [aws_subnet.lab-subnet-public-1.id]
  desired_capacity    = 1
  min_size            = 0
  max_size            = 1

  launch_template {
    id      = aws_launch_template.ecs_launch_template.id
    version = "$Latest"
  }
}


resource "aws_ecs_cluster" "cluster" {
  name = "ecs-lab-cluster"

  tags = {
    name = "ecs-cluster-name"
  }
}

resource "aws_ecs_task_definition" "task_definition" {
  container_definitions    = data.local_file.task_definition_json.content
  family                   = "ECS-Lab-Task-definition"
  network_mode             = "bridge"
  memory                   = "512"
  cpu                      = "512"
  requires_compatibilities = ["EC2"]
  task_role_arn            = aws_iam_role.ecs-task-role.arn

  pid_mode = "host"
  volume {
    name      = "modules"
    host_path = "/lib/modules"
  }
  volume {
    name      = "kernels"
    host_path = "/usr/src/kernels"
  }
}

data "local_file" "task_definition_json" {
  filename = "${path.module}/resources/ecs/task_definition.json"
  depends_on = [
    null_resource.rds_endpoint
  ]
}



resource "aws_ecs_service" "worker" {
  name                              = "ecs_service_worker"
  cluster                           = aws_ecs_cluster.cluster.id
  task_definition                   = aws_ecs_task_definition.task_definition.arn
  desired_count                     = 1
  health_check_grace_period_seconds = 2147483647

  load_balancer {
    target_group_arn = aws_lb_target_group.target_group.arn
    container_name   = "aws-goat-m2"
    container_port   = 80
  }
  depends_on = [aws_lb_listener.listener]
}

resource "aws_alb" "application_load_balancer" {
  name               = "aws-goat-m2-alb"
  internal           = false
  load_balancer_type = "application"
  subnets            = [aws_subnet.lab-subnet-public-1.id, aws_subnet.lab-subnet-public-1b.id]
  security_groups    = [aws_security_group.load_balancer_security_group.id]

  tags = {
    Name = "aws-goat-m2-alb"
  }
}

resource "aws_lb_target_group" "target_group" {
  name        = "aws-goat-m2-tg"
  port        = 80
  protocol    = "HTTP"
  target_type = "instance"
  vpc_id      = aws_vpc.lab-vpc.id

  tags = {
    Name = "aws-goat-m2-tg"
  }
}

resource "aws_lb_listener" "listener" {
  load_balancer_arn = aws_alb.application_load_balancer.id
  port              = "80"
  protocol          = "HTTP"

  default_action {
    type             = "forward"
    target_group_arn = aws_lb_target_group.target_group.id
  }
}


resource "aws_secretsmanager_secret" "rds_creds" {
  name                    = "RDS_CREDS"
  recovery_window_in_days = 0
}

resource "aws_secretsmanager_secret_version" "secret_version" {
  secret_id     = aws_secretsmanager_secret.rds_creds.id
  secret_string = <<EOF
   {
    "username": "root",
    "password": "T2kVB3zgeN3YbrKS"
   }
EOF
}

resource "null_resource" "rds_endpoint" {
  provisioner "local-exec" {
    command     = <<EOF
RDS_URL="${aws_db_instance.database-instance.endpoint}"
RDS_URL=$${RDS_URL::-5}
sed -i.bak "s,RDS_ENDPOINT_VALUE,$RDS_URL,g" ${path.module}/resources/ecs/task_definition.json
rm -f ${path.module}/resources/ecs/task_definition.json.bak
EOF
    interpreter = ["/bin/bash", "-c"]
  }

  depends_on = [
    aws_db_instance.database-instance
  ]
}

resource "null_resource" "cleanup" {
  provisioner "local-exec" {
    command     = <<EOF
RDS_URL="${aws_db_instance.database-instance.endpoint}"
RDS_URL=$${RDS_URL::-5}
sed -i.bak "s,$RDS_URL,RDS_ENDPOINT_VALUE,g" ${path.module}/resources/ecs/task_definition.json
rm -f ${path.module}/resources/ecs/task_definition.json.bak
EOF
    interpreter = ["/bin/bash", "-c"]
  }

  depends_on = [
    null_resource.rds_endpoint, aws_ecs_task_definition.task_definition
  ]
}


/* Creating a S3 Bucket for Terraform state file upload. */
resource "aws_s3_bucket" "bucket_tf_files" {
  bucket        = "do-not-delete-awsgoat-state-files-${data.aws_caller_identity.current.account_id}"
  force_destroy = true
  tags = {
    Name        = "Do not delete Bucket"
    Environment = "Dev"
  }
}

resource "aws_acm_certificate" "custom" {
  count             = local.create_custom_domain ? 1 : 0
  domain_name       = var.custom_domain_name
  validation_method = "DNS"

  lifecycle {
    create_before_destroy = true
  }
}

resource "aws_route53_record" "custom_cert_validation" {
  for_each = local.create_custom_domain ? {
    for dvo in aws_acm_certificate.custom[0].domain_validation_options :
    dvo.domain_name => {
      name   = dvo.resource_record_name
      record = dvo.resource_record_value
      type   = dvo.resource_record_type
    }
  } : {}

  allow_overwrite = true
  zone_id         = data.aws_route53_zone.custom[0].zone_id
  name            = each.value.name
  type            = each.value.type
  ttl             = 60
  records         = [each.value.record]
}

resource "aws_acm_certificate_validation" "custom" {
  count                   = local.create_custom_domain ? 1 : 0
  certificate_arn         = aws_acm_certificate.custom[0].arn
  validation_record_fqdns = [for r in aws_route53_record.custom_cert_validation : r.fqdn]
}

resource "aws_security_group_rule" "alb_https" {
  count             = local.create_custom_domain ? 1 : 0
  type              = "ingress"
  from_port         = 443
  to_port           = 443
  protocol          = "tcp"
  cidr_blocks       = ["0.0.0.0/0"]
  security_group_id = aws_security_group.load_balancer_security_group.id
}

resource "aws_lb_listener" "https" {
  count             = local.create_custom_domain ? 1 : 0
  load_balancer_arn = aws_alb.application_load_balancer.id
  port              = "443"
  protocol          = "HTTPS"
  ssl_policy        = "ELBSecurityPolicy-2016-08"
  certificate_arn   = aws_acm_certificate_validation.custom[0].certificate_arn

  default_action {
    type             = "forward"
    target_group_arn = aws_lb_target_group.target_group.id
  }
}

resource "aws_route53_record" "custom_alias" {
  count   = local.create_custom_domain ? 1 : 0
  zone_id = data.aws_route53_zone.custom[0].zone_id
  name    = var.custom_domain_name
  type    = "A"

  alias {
    name                   = aws_alb.application_load_balancer.dns_name
    zone_id                = aws_alb.application_load_balancer.zone_id
    evaluate_target_health = false
  }
}

output "ad_Target_URL" {
  value = "${aws_alb.application_load_balancer.dns_name}:80/login.php"
}

output "custom_app_url" {
  value = local.create_custom_domain ? "https://${var.custom_domain_name}/login.php" : null
}
