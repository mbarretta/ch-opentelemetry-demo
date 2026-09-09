# The AWS profile is never set here: scripts export AWS_PROFILE (from
# envvars.aws) and the provider picks it up from the environment, so the same
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
