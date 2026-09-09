# Nightly scale-to-zero. EventBridge Scheduler calls eks:UpdateNodegroupConfig
# directly through its universal target (no Lambda), so a forgotten demo costs
# at most one day of nodes. `./demo.sh nightly on|off` flips the schedule state
# at run time; scale_down_hour / scale_down_timezone move it.

data "aws_caller_identity" "current" {}

data "aws_iam_policy_document" "scheduler_assume" {
  statement {
    actions = ["sts:AssumeRole"]

    principals {
      type        = "Service"
      identifiers = ["scheduler.amazonaws.com"]
    }

    # Confused-deputy guard: only schedules in this account may assume the role.
    condition {
      test     = "StringEquals"
      variable = "aws:SourceAccount"
      values   = [data.aws_caller_identity.current.account_id]
    }
  }
}

resource "aws_iam_role" "scheduler" {
  name               = "${var.name}-scheduler"
  description        = "Assumed by EventBridge Scheduler to scale the ${var.name} demo node group to 0."
  assume_role_policy = data.aws_iam_policy_document.scheduler_assume.json
}

data "aws_iam_policy_document" "scheduler_scale" {
  statement {
    actions   = ["eks:UpdateNodegroupConfig"]
    resources = [module.eks.eks_managed_node_groups["demo"].node_group_arn]
  }
}

resource "aws_iam_role_policy" "scheduler_scale" {
  name   = "scale-demo-nodegroup"
  role   = aws_iam_role.scheduler.id
  policy = data.aws_iam_policy_document.scheduler_scale.json
}

resource "aws_scheduler_schedule" "nightly_scale_down" {
  name        = "${var.name}-nightly-scale-down"
  description = "Scale the ${var.name} demo node group to 0 every night."
  state       = var.nightly_scale_down_enabled ? "ENABLED" : "DISABLED"

  schedule_expression          = "cron(0 ${var.scale_down_hour} * * ? *)"
  schedule_expression_timezone = var.scale_down_timezone

  flexible_time_window {
    mode = "OFF"
  }

  target {
    arn      = "arn:aws:scheduler:::aws-sdk:eks:updateNodegroupConfig"
    role_arn = aws_iam_role.scheduler.arn

    # The EKS UpdateNodegroupConfig request body, camelCase as the API expects.
    input = jsonencode({
      clusterName   = module.eks.cluster_name
      nodegroupName = local.nodegroup_name
      scalingConfig = {
        minSize     = 0
        maxSize     = var.node_count
        desiredSize = 0
      }
    })

    retry_policy {
      maximum_retry_attempts       = 3
      maximum_event_age_in_seconds = 3600
    }
  }
}
