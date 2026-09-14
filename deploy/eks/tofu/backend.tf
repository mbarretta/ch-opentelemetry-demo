# Remote state in S3 with native S3 locking (no DynamoDB table).
#
# Bucket, key and region are deliberately absent: they are account-specific and
# arrive from `demo.py eks init` as `-backend-config=` flags
# (bucket=otel-demo-eks-tfstate-<account-id>, key=otel-demo-eks/terraform.tfstate,
# region=<AWS_REGION>). The control plane is long-lived and costs money, so state
# must not live on one laptop where it could orphan the cluster.
terraform {
  backend "s3" {
    use_lockfile = true
  }
}
