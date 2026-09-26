variable "region" {
  type        = string
  description = "AWS deployment region."
  default     = "ap-southeast-2"
}

variable "project_name" {
  type        = string
  description = "Prefix for all AWS resource names."
  default     = "quote0-busboard"
}

variable "tfnsw_api_key" {
  type        = string
  sensitive   = true
  description = "TfNSW Open Data API key."
}

variable "quote0_api_key" {
  type        = string
  sensitive   = true
  description = "Dot App API key."
}

variable "quote0_device_id" {
  type        = string
  description = "Quote/0 device serial number."
}

variable "quote0_task_key" {
  type        = string
  description = "Optional Image API task key."
  default     = ""
}

variable "stop_id" {
  type    = string
  default = "212711"
}

variable "route_number" {
  type    = string
  default = "526"
}

variable "destination_filter" {
  type    = string
  default = ""
}

variable "max_departures" {
  type    = number
  default = 3
  validation {
    condition     = var.max_departures >= 1 && var.max_departures <= 3
    error_message = "max_departures must be between 1 and 3."
  }
}

variable "timezone" {
  type    = string
  default = "Australia/Sydney"
}

variable "active_start" {
  type    = string
  default = "10:00"
}

variable "active_end" {
  type    = string
  default = "19:00"
}

variable "normal_refresh_minutes" {
  type    = number
  default = 10
  validation {
    condition     = var.normal_refresh_minutes > 0 && var.normal_refresh_minutes % 2 == 0
    error_message = "normal_refresh_minutes must be a positive multiple of 2."
  }
}

variable "peak_windows" {
  type    = string
  default = "16:30-18:30"
}

variable "peak_refresh_minutes" {
  type    = number
  default = 2
  validation {
    condition     = var.peak_refresh_minutes > 0 && var.peak_refresh_minutes % 2 == 0
    error_message = "peak_refresh_minutes must be a positive multiple of 2."
  }
}

variable "log_retention_days" {
  type    = number
  default = 30
}
