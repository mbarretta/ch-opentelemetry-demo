# Registries for the four images `demo.py publish` pushes: the patched frontend,
# the front proxy, and the assistant's agent and MCP services. One repository
# per service under a shared `ch-opentelemetry-demo/` prefix, created with
# for_each so the set below is the only place a service is named.
#
# Tags are MUTABLE so a rebuilt image with the same content-derived tag can be
# re-pushed; force_delete lets `tofu destroy` remove a non-empty repository.
# Basic (Amazon ECR native) scanning runs on every push at no charge; it is
# per-repository and does not enable the account-wide, billable enhanced scanning.
locals {
  # Keep in step with IMAGES in launcher/core.py: `demo.py publish` pushes one
  # image per key and `demo.py eks status` looks for the current tag in each.
  ecr_services = toset(["frontend", "frontend-proxy", "agent", "mcp"])
}

resource "aws_ecr_repository" "demo" {
  for_each = local.ecr_services

  name                 = "ch-opentelemetry-demo/${each.key}"
  image_tag_mutability = "MUTABLE"
  force_delete         = true

  image_scanning_configuration {
    scan_on_push = true
  }
}

resource "aws_ecr_lifecycle_policy" "demo" {
  for_each = aws_ecr_repository.demo

  repository = each.value.name

  policy = jsonencode({
    rules = [
      {
        rulePriority = 1
        description  = "Keep only the 5 most recent images"
        selection = {
          tagStatus   = "any"
          countType   = "imageCountMoreThan"
          countNumber = 5
        }
        action = {
          type = "expire"
        }
      }
    ]
  })
}
