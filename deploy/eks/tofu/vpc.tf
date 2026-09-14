# Network. Two modes, chosen by use_default_vpc:
#
#   true (default)  The cluster lands in the account's DEFAULT VPC
#                   (172.31.0.0/16 in us-east-1), in the default-for-AZ public
#                   subnets of two EKS-capable AZs. Nothing here is created or
#                   managed by OpenTofu: the VPC and subnets are read with data
#                   sources only, so `tofu destroy` can never touch them. This is
#                   what an account at its VPC quota needs, and there is no
#                   network to tear down afterwards.
#   false           A dedicated 10.20.0.0/16 VPC from terraform-aws-modules/vpc
#                   with two public subnets, for accounts with VPC headroom.
#
# Either way it is public subnets only. ClickHouse Cloud accepts connections
# from any IP and the storefront is reached over kubectl port-forward, so nodes
# sit in public subnets with public IPs and there is no NAT gateway, private
# subnet or VPC endpoint to pay for. map_public_ip_on_launch is mandatory:
# without a public IP a node in a NAT-less subnet can never reach the EKS API
# or pull images.
#
# Never add the provider's aws_default_* resources (default VPC, subnet,
# security group, route table, network ACL) here: those ADOPT the account
# defaults into state, and a later destroy or drift would modify or delete
# infrastructure shared with everything else in the account. The EKS module
# creates its own security groups; the default security group is never
# referenced.

data "aws_availability_zones" "available" {
  state = "available"

  # AZs that cannot host an EKS control plane (us-east-1e) or that have no
  # default subnet in the account.
  exclude_names = var.eks_excluded_azs

  # Exclude Local Zones and Wavelength Zones, which cannot host EKS nodes.
  filter {
    name   = "opt-in-status"
    values = ["opt-in-not-required"]
  }
}

locals {
  vpc_cidr = "10.20.0.0/16"
  azs      = slice(data.aws_availability_zones.available.names, 0, 2)

  vpc_id     = var.use_default_vpc ? data.aws_vpc.default[0].id : module.vpc[0].vpc_id
  subnet_ids = var.use_default_vpc ? [for az in local.azs : data.aws_subnet.default[az].id] : module.vpc[0].public_subnets
}

# --- default VPC (use_default_vpc = true) --------------------------------------

data "aws_vpc" "default" {
  count = var.use_default_vpc ? 1 : 0

  default = true
}

# One default-for-AZ subnet per chosen AZ. Keyed on the AZ name, which is a
# data-source result known at plan time, so for_each is fine here.
data "aws_subnet" "default" {
  for_each = toset(var.use_default_vpc ? local.azs : [])

  filter {
    name   = "vpc-id"
    values = [data.aws_vpc.default[0].id]
  }

  filter {
    name   = "default-for-az"
    values = ["true"]
  }

  filter {
    name   = "availability-zone"
    values = [each.key]
  }

  lifecycle {
    # Default subnets assign public IPs out of the box; if someone turned that
    # off, nodes placed there would come up with no route to the EKS API or the
    # registries (there is no NAT gateway). Fail the plan instead.
    postcondition {
      condition     = self.map_public_ip_on_launch
      error_message = "Default subnet in ${each.key} does not assign public IPs on launch. Nodes there could not reach the EKS API or pull images (no NAT gateway); re-enable map-public-ip-on-launch on it or set use_default_vpc = false."
    }
  }
}

# --- dedicated VPC (use_default_vpc = false) ----------------------------------

module "vpc" {
  source  = "terraform-aws-modules/vpc/aws"
  version = "~> 6.7"

  count = var.use_default_vpc ? 0 : 1

  name = var.name
  cidr = local.vpc_cidr

  azs = local.azs
  # One /20 per AZ (4094 addresses each); the VPC CNI hands a VPC IP to every pod.
  public_subnets = [for i in range(length(local.azs)) : cidrsubnet(local.vpc_cidr, 4, i)]

  enable_nat_gateway      = false
  map_public_ip_on_launch = true
  enable_dns_hostnames    = true
}
