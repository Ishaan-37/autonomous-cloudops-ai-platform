variable "aws_region" {
  type    = string
  default = "ap-south-1"
}

variable "environment" {
  type    = string
  default = "staging"
}

variable "alert_email" {
  type = string
}

variable "api_webhook_url" {
  type = string
}