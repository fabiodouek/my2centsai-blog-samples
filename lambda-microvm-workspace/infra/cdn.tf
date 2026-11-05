# One CloudFront distribution: the SPA from a private S3 bucket (OAC), /api/* and /ws/* to the ALB
# with the origin secret header. Same origin for the browser, so no CORS and WebSockets just work.

resource "aws_s3_bucket" "spa" {
  bucket_prefix = "${local.name}-spa-"
  force_destroy = true
}

resource "aws_s3_bucket_public_access_block" "spa" {
  bucket                  = aws_s3_bucket.spa.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_policy" "spa" {
  bucket = aws_s3_bucket.spa.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Sid       = "CloudFrontRead"
      Effect    = "Allow"
      Principal = { Service = "cloudfront.amazonaws.com" }
      Action    = "s3:GetObject"
      Resource  = "${aws_s3_bucket.spa.arn}/*"
      Condition = { StringEquals = { "AWS:SourceArn" = aws_cloudfront_distribution.cdn.arn } }
    }]
  })
  depends_on = [aws_s3_bucket_public_access_block.spa]
}

resource "aws_cloudfront_origin_access_control" "spa" {
  name                              = "${local.name}-spa"
  origin_access_control_origin_type = "s3"
  signing_behavior                  = "always"
  signing_protocol                  = "sigv4"
}

data "aws_cloudfront_cache_policy" "disabled" {
  name = "Managed-CachingDisabled"
}

data "aws_cloudfront_cache_policy" "optimized" {
  name = "Managed-CachingOptimized"
}

data "aws_cloudfront_origin_request_policy" "all_viewer_except_host" {
  name = "Managed-AllViewerExceptHostHeader"
}

# SPA fallback on the S3 behavior only. Distribution-wide custom error responses (403/404 ->
# /index.html) would also rewrite the API's own 403/404 JSON into index.html with a 200.
resource "aws_cloudfront_function" "spa_fallback" {
  name    = "${local.name}-spa-fallback"
  runtime = "cloudfront-js-2.0"
  publish = true
  code    = <<-EOT
    function handler(event) {
      var req = event.request;
      // A path whose last segment has no dot is a client-side route
      if (req.uri.split('/').pop().indexOf('.') === -1) {
        req.uri = '/index.html';
      }
      return req;
    }
  EOT
}

locals {
  s3_origin  = "spa-s3"
  alb_origin = "api-alb"
}

resource "aws_cloudfront_distribution" "cdn" {
  enabled             = true
  comment             = "my2cents Lambda MicroVM workspace"
  default_root_object = "index.html"
  price_class         = "PriceClass_100"
  http_version        = "http2"

  origin {
    origin_id                = local.s3_origin
    domain_name              = aws_s3_bucket.spa.bucket_regional_domain_name
    origin_access_control_id = aws_cloudfront_origin_access_control.spa.id
  }

  origin {
    origin_id   = local.alb_origin
    domain_name = aws_lb.alb.dns_name
    custom_origin_config {
      http_port                = 80
      https_port               = 443
      origin_protocol_policy   = "http-only"
      origin_ssl_protocols     = ["TLSv1.2"]
      origin_read_timeout      = 60
      origin_keepalive_timeout = 60
    }
    custom_header {
      name  = "X-Origin-Verify"
      value = random_password.origin_secret.result
    }
  }

  default_cache_behavior {
    target_origin_id       = local.s3_origin
    viewer_protocol_policy = "redirect-to-https"
    allowed_methods        = ["GET", "HEAD", "OPTIONS"]
    cached_methods         = ["GET", "HEAD"]
    compress               = true
    cache_policy_id        = data.aws_cloudfront_cache_policy.optimized.id
    function_association {
      event_type   = "viewer-request"
      function_arn = aws_cloudfront_function.spa_fallback.arn
    }
  }

  dynamic "ordered_cache_behavior" {
    for_each = ["/api/*", "/ws/*"]
    content {
      path_pattern             = ordered_cache_behavior.value
      target_origin_id         = local.alb_origin
      viewer_protocol_policy   = "https-only"
      allowed_methods          = ["GET", "HEAD", "OPTIONS", "PUT", "POST", "PATCH", "DELETE"]
      cached_methods           = ["GET", "HEAD"]
      compress                 = false
      cache_policy_id          = data.aws_cloudfront_cache_policy.disabled.id
      origin_request_policy_id = data.aws_cloudfront_origin_request_policy.all_viewer_except_host.id
    }
  }

  restrictions {
    geo_restriction {
      restriction_type = "none"
    }
  }

  viewer_certificate {
    cloudfront_default_certificate = true
  }
}
