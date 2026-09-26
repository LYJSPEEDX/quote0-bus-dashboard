provider "aws" {
  region = var.region
}

locals {
  function_name = var.project_name
  common_tags = {
    Project   = var.project_name
    ManagedBy = "Terraform"
  }
  lambda_environment = {
    TFNSW_API_KEY          = var.tfnsw_api_key
    QUOTE0_API_KEY         = var.quote0_api_key
    QUOTE0_DEVICE_ID       = var.quote0_device_id
    LOCATIONS              = jsonencode(var.locations)
    MAX_DEPARTURES         = tostring(var.max_departures)
    TIMEZONE               = var.timezone
    ACTIVE_START           = var.active_start
    ACTIVE_END             = var.active_end
    NORMAL_REFRESH_MINUTES = tostring(var.normal_refresh_minutes)
    PEAK_WINDOWS           = var.peak_windows
    PEAK_REFRESH_MINUTES   = tostring(var.peak_refresh_minutes)
    REFRESH_MODE           = var.refresh_mode
  }
}

data "aws_caller_identity" "current" {}

resource "aws_cloudwatch_log_group" "lambda" {
  name              = "/aws/lambda/${local.function_name}"
  retention_in_days = var.log_retention_days
  tags              = local.common_tags
}

resource "aws_iam_role" "lambda" {
  name = "${local.function_name}-lambda-role"
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "lambda.amazonaws.com" }
      Action    = "sts:AssumeRole"
    }]
  })
  tags = local.common_tags
}

resource "aws_iam_role_policy" "lambda_logs" {
  name = "${local.function_name}-logs"
  role = aws_iam_role.lambda.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect   = "Allow"
      Action   = ["logs:CreateLogStream", "logs:PutLogEvents"]
      Resource = "${aws_cloudwatch_log_group.lambda.arn}:*"
    }]
  })
}

resource "aws_lambda_function" "board" {
  function_name    = local.function_name
  description      = "Sydney 526 departures for Quote/0"
  filename         = "${path.module}/../build/lambda.zip"
  source_code_hash = filebase64sha256("${path.module}/../build/lambda.zip")
  handler          = "app.lambda_handler"
  runtime          = "python3.11"
  architectures    = ["x86_64"]
  role             = aws_iam_role.lambda.arn
  memory_size      = 256
  timeout          = 30
  tags             = local.common_tags

  environment {
    variables = local.lambda_environment
  }

  depends_on = [aws_cloudwatch_log_group.lambda, aws_iam_role_policy.lambda_logs]
}

resource "aws_iam_role" "scheduler" {
  name = "${local.function_name}-scheduler-role"
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "scheduler.amazonaws.com" }
      Action    = "sts:AssumeRole"
    }]
  })
  tags = local.common_tags
}

resource "aws_iam_role_policy" "scheduler_invoke" {
  name = "${local.function_name}-invoke"
  role = aws_iam_role.scheduler.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect   = "Allow"
      Action   = "lambda:InvokeFunction"
      Resource = aws_lambda_function.board.arn
    }]
  })
}

# The event fires every two minutes in the configured daytime envelope. The
# Lambda gates normal and peak refreshes, so no TfNSW or Quote/0 calls occur on
# non-due invocations. Scheduler's time zone handles Sydney daylight saving.
resource "aws_scheduler_schedule" "refresh" {
  name                         = "${local.function_name}-refresh"
  schedule_expression          = "cron(0/2 10-18 ? * * *)"
  schedule_expression_timezone = var.timezone
  state                        = "ENABLED"

  flexible_time_window { mode = "OFF" }

  target {
    arn      = aws_lambda_function.board.arn
    role_arn = aws_iam_role.scheduler.arn
    input    = jsonencode({ source = "eventbridge-scheduler" })

    retry_policy {
      maximum_event_age_in_seconds = 60
      maximum_retry_attempts       = 0
    }
  }
}

# Restricts the function resource policy to this exact Scheduler ARN in addition
# to the Scheduler role's identity-based invoke permission.
resource "aws_lambda_permission" "scheduler" {
  statement_id  = "AllowQuote0BusboardScheduler"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.board.function_name
  principal     = "scheduler.amazonaws.com"
  source_arn    = aws_scheduler_schedule.refresh.arn
}
