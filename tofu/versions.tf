# Version pins. `use_lockfile` on the S3 backend (backend.tf) needs OpenTofu
# 1.10+; terraform-aws-modules/eks ~> 21.25 needs hashicorp/aws >= 6.59.
terraform {
  required_version = ">= 1.10"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = ">= 6.59, < 7.0"
    }
  }
}
