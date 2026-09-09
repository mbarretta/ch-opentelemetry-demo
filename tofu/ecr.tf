# Registry for the session-replay frontend image built by scripts/build-frontend.sh.
# Tags are MUTABLE so a rebuilt image with the same deterministic tag can be
# re-pushed; force_delete lets `tofu destroy` remove a non-empty repository.
resource "aws_ecr_repository" "frontend" {
  name                 = "otel-demo-frontend"
  image_tag_mutability = "MUTABLE"
  force_delete         = true

  image_scanning_configuration {
    scan_on_push = false
  }
}

resource "aws_ecr_lifecycle_policy" "frontend" {
  repository = aws_ecr_repository.frontend.name

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
