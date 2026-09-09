variable "region" {
  description = "AWS region for every resource."
  type        = string
  default     = "us-east-1"
}

variable "name" {
  description = "Name of the EKS cluster; also prefixes the VPC, IAM role and schedule."
  type        = string
  default     = "otel-demo-eks"
}

variable "kubernetes_version" {
  description = "EKS Kubernetes version. Keep within one minor of the local kubectl."
  type        = string
  default     = "1.36"
}

# --- node group ----------------------------------------------------------------
#
# ami_type, instance_type and node_platform move together: the frontend image
# is built for node_platform and must run on the CPU architecture the AMI and
# instance family provide. Default is Graviton (arm64) so the image builds
# natively on Apple Silicon. x86 alternative: ami_type = "AL2023_x86_64_STANDARD",
# instance_type = "m7i.xlarge", node_platform = "linux/amd64" (cross-built under
# QEMU on an arm64 Mac).

variable "ami_type" {
  description = "EKS managed node group AMI type. Must match node_platform (validated there)."
  type        = string
  default     = "AL2023_ARM_64_STANDARD"
}

variable "instance_type" {
  description = "EC2 instance type for the demo node group. m7g.xlarge (4 vCPU / 16 GiB) fits the ~30-pod demo on two nodes."
  type        = string
  default     = "m7g.xlarge"
}

variable "node_platform" {
  description = "Docker platform the frontend image is built for; must match the CPU architecture of ami_type."
  type        = string
  default     = "linux/arm64"

  validation {
    condition     = contains(["linux/arm64", "linux/amd64"], var.node_platform)
    error_message = "node_platform must be \"linux/arm64\" or \"linux/amd64\"."
  }

  validation {
    condition = !(
      (strcontains(var.ami_type, "ARM_64") && var.node_platform == "linux/amd64") ||
      (strcontains(var.ami_type, "x86_64") && var.node_platform == "linux/arm64")
    )
    error_message = "node_platform must match the CPU architecture of ami_type: *_ARM_64_* AMIs need linux/arm64 and *_x86_64_* AMIs need linux/amd64."
  }
}

variable "node_count" {
  description = "Nodes while the demo is up (desired and max size). Scripts and the nightly schedule scale to 0 when idle."
  type        = number
  default     = 2

  validation {
    condition     = var.node_count >= 1 && floor(var.node_count) == var.node_count
    error_message = "node_count must be a whole number of at least 1."
  }
}

variable "root_volume_gb" {
  description = "Root EBS volume size (GiB, gp3) for each node."
  type        = number
  default     = 50
}

# --- access --------------------------------------------------------------------

variable "endpoint_public_access_cidrs" {
  description = "CIDRs allowed to reach the public EKS API endpoint (IAM-authenticated as usual). Narrow this to your egress IPs if you can."
  type        = list(string)
  default     = ["0.0.0.0/0"]
}

variable "extra_admin_principal_arns" {
  description = "IAM principal ARNs (for example teammates' SSO permission-set roles) that get AmazonEKSClusterAdminPolicy in addition to the identity that runs tofu apply."
  type        = list(string)
  default     = []
}

# --- nightly scale-to-zero -----------------------------------------------------

variable "scale_down_hour" {
  description = "Hour (0-23, in scale_down_timezone) at which EventBridge Scheduler scales the node group to 0 every day."
  type        = number
  default     = 23

  validation {
    condition     = var.scale_down_hour >= 0 && var.scale_down_hour <= 23 && floor(var.scale_down_hour) == var.scale_down_hour
    error_message = "scale_down_hour must be a whole number from 0 to 23."
  }
}

variable "scale_down_timezone" {
  description = "IANA timezone for scale_down_hour."
  type        = string
  default     = "America/New_York"
}

variable "nightly_scale_down_enabled" {
  description = "Whether the nightly scale-to-zero schedule is ENABLED. `./demo.sh nightly off` toggles it at run time without a tofu apply."
  type        = bool
  default     = true
}
