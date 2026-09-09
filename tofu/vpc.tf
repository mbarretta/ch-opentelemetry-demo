# Public subnets only. ClickHouse Cloud accepts connections from any IP and the
# storefront is reached over kubectl port-forward, so nodes sit in public
# subnets with public IPs and there is no NAT gateway, private subnet or VPC
# endpoint to pay for. map_public_ip_on_launch is mandatory: without a public
# IP a node in a NAT-less subnet can never reach the EKS API or pull images.

data "aws_availability_zones" "available" {
  state = "available"

  # Exclude Local Zones and Wavelength Zones, which cannot host EKS nodes.
  filter {
    name   = "opt-in-status"
    values = ["opt-in-not-required"]
  }
}

locals {
  vpc_cidr = "10.20.0.0/16"
  azs      = slice(data.aws_availability_zones.available.names, 0, 2)
}

module "vpc" {
  source  = "terraform-aws-modules/vpc/aws"
  version = "~> 6.7"

  name = var.name
  cidr = local.vpc_cidr

  azs = local.azs
  # One /20 per AZ (4094 addresses each); the VPC CNI hands a VPC IP to every pod.
  public_subnets = [for i in range(length(local.azs)) : cidrsubnet(local.vpc_cidr, 4, i)]

  enable_nat_gateway      = false
  map_public_ip_on_launch = true
  enable_dns_hostnames    = true
}
