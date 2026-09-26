terraform {
  # 1.10+ for S3-native state locking (use_lockfile).
  required_version = ">= 1.10.0"

  # Remote state in S3. The bucket is account-specific, so its settings live in
  # the git-ignored backend.hcl (see backend.hcl.example and docs/HANDBOOK.md):
  #   terraform init -backend-config=backend.hcl
  backend "s3" {}

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.0"
    }
  }
}
