# The AWS profile is never set here: `demo.py eks ...` exports AWS_PROFILE (from
# the root .env) and the provider picks it up from the environment, so the same
# config works for every presenter's SSO profile.
provider "aws" {
  region = var.region

  default_tags {
    tags = {
      Project   = "otel-demo-eks"
      ManagedBy = "opentofu"
    }
  }
}
