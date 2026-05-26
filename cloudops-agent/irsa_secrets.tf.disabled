# irsa_secrets.tf
# ---------------
# IAM Role for Service Accounts (IRSA) — lets the EKS pod read Secrets Manager
# without any long-lived AWS access keys mounted in the container.

data "aws_caller_identity" "current" {}
data "aws_region" "current" {}

# Trust policy: only the specific Kubernetes service account can assume this role
data "aws_iam_policy_document" "irsa_trust" {
  statement {
    actions = ["sts:AssumeRoleWithWebIdentity"]
    principals {
      type        = "Federated"
      identifiers = [module.eks.oidc_provider_arn]
    }
    condition {
      test     = "StringEquals"
      variable = "${module.eks.oidc_provider}:sub"
      values   = ["system:serviceaccount:cloudops-prod:cloudops-agent"]
    }
    condition {
      test     = "StringEquals"
      variable = "${module.eks.oidc_provider}:aud"
      values   = ["sts.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "agent_irsa" {
  name               = "cloudops-agent-irsa-role"
  assume_role_policy = data.aws_iam_policy_document.irsa_trust.json
}

# Permissions: read secrets + CloudWatch + EC2 + SSM (least privilege)
resource "aws_iam_role_policy" "agent_permissions" {
  name = "cloudops-agent-permissions"
  role = aws_iam_role.agent_irsa.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid    = "ReadSecrets"
        Effect = "Allow"
        Action = ["secretsmanager:GetSecretValue"]
        Resource = [
          "arn:aws:secretsmanager:${data.aws_region.current.name}:${data.aws_caller_identity.current.account_id}:secret:cloudops-agent/*"
        ]
      },
      {
        Sid    = "CloudWatchLogs"
        Effect = "Allow"
        Action = [
          "logs:GetLogEvents",
          "logs:FilterLogEvents",
          "logs:DescribeLogGroups",
          "logs:DescribeLogStreams",
          "cloudwatch:GetMetricData",
          "cloudwatch:DescribeAlarms"
        ]
        Resource = "*"
      },
      {
        Sid    = "EC2ReadAndRemediate"
        Effect = "Allow"
        Action = [
          "ec2:DescribeInstances",
          "ec2:DescribeInstanceStatus",
          "ec2:RebootInstances"
        ]
        Resource = "*"
      },
      {
        Sid    = "SSMRunCommand"
        Effect = "Allow"
        Action = [
          "ssm:SendCommand",
          "ssm:GetCommandInvocation",
          "ssm:ListCommandInvocations"
        ]
        Resource = [
          "arn:aws:ssm:*:*:document/AWS-RunShellScript",
          "arn:aws:ec2:*:*:instance/*"
        ]
      },
      {
        Sid    = "CostExplorer"
        Effect = "Allow"
        Action = ["ce:GetCostAndUsage", "trustedadvisor:Describe*"]
        Resource = "*"
      }
    ]
  })
}
