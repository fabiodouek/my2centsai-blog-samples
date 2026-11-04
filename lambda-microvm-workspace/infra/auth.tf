# Cognito: email sign-in, admin-created users only (scripts/create_user.sh), managed login pages,
# one public PKCE client for the SPA.

resource "aws_cognito_user_pool" "pool" {
  name                     = local.name
  user_pool_tier           = "ESSENTIALS" # managed login (v2) needs Essentials or Plus
  username_attributes      = ["email"]
  auto_verified_attributes = ["email"]
  deletion_protection      = "INACTIVE"
  mfa_configuration        = "OFF"

  username_configuration {
    case_sensitive = false
  }
  admin_create_user_config {
    allow_admin_create_user_only = true
  }
  account_recovery_setting {
    recovery_mechanism {
      name     = "verified_email"
      priority = 1
    }
  }
}

resource "aws_cognito_user_pool_client" "spa" {
  name                                 = "${local.name}-spa"
  user_pool_id                         = aws_cognito_user_pool.pool.id
  generate_secret                      = false
  allowed_oauth_flows_user_pool_client = true
  allowed_oauth_flows                  = ["code"]
  allowed_oauth_scopes                 = ["openid", "email"]
  supported_identity_providers         = ["COGNITO"]
  callback_urls                        = concat(["https://${aws_cloudfront_distribution.cdn.domain_name}/"], var.extra_callback_urls)
  logout_urls                          = concat(["https://${aws_cloudfront_distribution.cdn.domain_name}/"], var.extra_callback_urls)
  # USER_PASSWORD_AUTH lets scripts get an ID token without a browser (initiate-auth)
  explicit_auth_flows           = ["ALLOW_USER_SRP_AUTH", "ALLOW_USER_PASSWORD_AUTH", "ALLOW_REFRESH_TOKEN_AUTH"]
  prevent_user_existence_errors = "ENABLED"
  enable_token_revocation       = true
  id_token_validity             = 60
  access_token_validity         = 60
  refresh_token_validity        = 30
  token_validity_units {
    id_token      = "minutes"
    access_token  = "minutes"
    refresh_token = "days"
  }
}

resource "aws_cognito_user_pool_domain" "login" {
  domain                = "${local.name}-${random_id.suffix.hex}"
  user_pool_id          = aws_cognito_user_pool.pool.id
  managed_login_version = 2
}

# Managed login (v2) shows an error page for clients without a style; use Cognito's default look
resource "aws_cognito_managed_login_branding" "spa" {
  user_pool_id                = aws_cognito_user_pool.pool.id
  client_id                   = aws_cognito_user_pool_client.spa.id
  use_cognito_provided_values = true
}

# Members may change the proxy policy (allowlist, credentials, AWS access) for every sandbox on the proxy.
# Add a user with: scripts/create_user.sh --admin <email>
resource "aws_cognito_user_group" "admins" {
  name         = "admins"
  user_pool_id = aws_cognito_user_pool.pool.id
  description  = "May change the iron-proxy policy from the workspace"
}
