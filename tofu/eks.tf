# EKS cluster with one managed node group on Graviton. The control plane stays
# up when idle (~$73/month); the node group scales between 0 and node_count.
#
# Deliberate cost trims: no control-plane CloudWatch logs, no customer-managed
# KMS key (EKS encrypts secrets with an AWS-owned key), no IRSA OIDC provider
# (nothing in the demo assumes an IAM role). Do not add EBS CSI, metrics-server
# or pod identity: with Jaeger/Prometheus/Grafana/OpenSearch disabled nothing
# needs a PersistentVolume.

locals {
  # Referenced by scheduler.tf and outputs.tf as well; the map key below stays
  # the literal `demo` so `["demo"]` lookups read plainly.
  nodegroup_name = "demo"
}

module "eks" {
  source  = "terraform-aws-modules/eks/aws"
  version = "~> 21.25"

  name               = var.name
  kubernetes_version = var.kubernetes_version

  vpc_id                   = module.vpc.vpc_id
  subnet_ids               = module.vpc.public_subnets
  control_plane_subnet_ids = module.vpc.public_subnets

  # The module defaults to a private-only endpoint; presenters reach the API
  # from their laptops, so the public endpoint is on (IAM-authenticated).
  endpoint_public_access       = true
  endpoint_public_access_cidrs = var.endpoint_public_access_cidrs

  # Access entries only (no aws-auth ConfigMap). The identity running
  # `tofu apply` (the SSO permission-set role) becomes cluster admin; teammates
  # are added through extra_admin_principal_arns.
  authentication_mode                      = "API"
  enable_cluster_creator_admin_permissions = true
  access_entries = {
    for arn in var.extra_admin_principal_arns : arn => {
      principal_arn = arn
      policy_associations = {
        admin = {
          policy_arn = "arn:aws:eks::aws:cluster-access-policy/AmazonEKSClusterAdminPolicy"
          access_scope = {
            type = "cluster"
          }
        }
      }
    }
  }

  addons = {
    coredns    = {}
    kube-proxy = {}
    vpc-cni = {
      # Install the CNI before the node group so nodes come up with networking
      # already configured instead of being re-patched afterwards.
      before_compute = true
    }
  }

  enabled_log_types           = []
  create_cloudwatch_log_group = false
  # Both are needed to skip the CMK: with create_kms_key = false alone the
  # module still renders an encryption_config block whose key_arn is null.
  create_kms_key    = false
  encryption_config = null
  enable_irsa       = false

  # The initial apply brings nodes up at node_count so the addons reach ACTIVE
  # (an addon create at zero nodes waits for its 20-minute timeout). The
  # node-group submodule ignores desired_size drift, so the scripts and the
  # nightly schedule can scale to 0 without a later `tofu apply` fighting them.
  eks_managed_node_groups = {
    demo = {
      name            = local.nodegroup_name
      use_name_prefix = false

      ami_type       = var.ami_type
      instance_types = [var.instance_type]
      capacity_type  = "ON_DEMAND"

      min_size     = 0
      max_size     = var.node_count
      desired_size = var.node_count

      subnet_ids = module.vpc.public_subnets

      block_device_mappings = {
        xvda = {
          device_name = "/dev/xvda"
          ebs = {
            volume_size           = var.root_volume_gb
            volume_type           = "gp3"
            delete_on_termination = true
          }
        }
      }
    }
  }
}
