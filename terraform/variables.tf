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

variable "locations" {
  description = "Screens to render. Each is one route at one or two stops (one row per direction), pushed to its own Quote/0 Image API task."
  type = list(object({
    name     = string
    route    = string
    task_key = optional(string, "")
    stops = list(object({
      id    = string
      label = string
    }))
  }))
  default = [{
    name  = "Olympic Park"
    route = "526"
    stops = [
      { id = "212726", label = "Strathfield" },
      { id = "212727", label = "Rhodes" },
    ]
  }]

  validation {
    condition     = length(var.locations) >= 1 && alltrue([for l in var.locations : length(l.stops) >= 1 && length(l.stops) <= 2])
    error_message = "Provide at least one location, each with one or two stops."
  }

  validation {
    condition = length(var.locations) == 1 || (
      alltrue([for l in var.locations : l.task_key != ""]) &&
      length(distinct([for l in var.locations : l.task_key])) == length(var.locations)
    )
    error_message = "With several locations, each needs a unique task_key."
  }
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
