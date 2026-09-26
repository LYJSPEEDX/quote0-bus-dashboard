output "lambda_function_name" {
  value = aws_lambda_function.board.function_name
}

output "lambda_function_arn" {
  value = aws_lambda_function.board.arn
}

output "scheduler_name" {
  value = aws_scheduler_schedule.refresh.name
}

output "aws_account_id" {
  value = data.aws_caller_identity.current.account_id
}
