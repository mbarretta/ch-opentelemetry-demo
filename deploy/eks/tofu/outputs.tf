# Read by scripts/lib/aws.sh via `tofu -chdir=tofu output -raw <name>`; every
# value the scripts read must therefore be a string, number or bool. subnet_ids
# is the one list: it is for humans and `tofu output -json`, not for tf_out.

output "region" {
  description = "AWS region."
  value       = var.region
}

output "cluster_name" {
  description = "EKS cluster name."
  value       = module.eks.cluster_name
}

output "cluster_endpoint" {
  description = "EKS API server endpoint."
  value       = module.eks.cluster_endpoint
}

output "nodegroup_name" {
  description = "Name of the managed node group the scripts scale."
  value       = local.nodegroup_name
}

output "node_count" {
  description = "Node count while up; also the node group's max size."
  value       = var.node_count
}

output "node_platform" {
  description = "Docker platform for the frontend image build."
  value       = var.node_platform
}

output "ecr_repository_url" {
  description = "ECR repository URL for the frontend image (<registry>/<repo>)."
  value       = aws_ecr_repository.frontend.repository_url
}

output "ecr_registry" {
  description = "ECR registry host (for docker login)."
  value       = split("/", aws_ecr_repository.frontend.repository_url)[0]
}

output "scheduler_name" {
  description = "Name of the nightly scale-to-zero schedule."
  value       = aws_scheduler_schedule.nightly_scale_down.name
}

output "kubeconfig_command" {
  description = "Command that writes a kubeconfig entry for the cluster."
  value       = "aws eks update-kubeconfig --region ${var.region} --name ${module.eks.cluster_name}"
}

output "vpc_id" {
  description = "VPC the cluster runs in: the account default VPC, or the dedicated one when use_default_vpc = false."
  value       = local.vpc_id
}

output "subnet_ids" {
  description = "Public subnets (one per chosen AZ) used by the control plane and the node group."
  value       = local.subnet_ids
}
