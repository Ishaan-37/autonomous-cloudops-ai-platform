#!/bin/bash
# =============================================================
# PHASE 2 STEP-BY-STEP COMMANDS
# Run these IN ORDER. Read each comment before running.
# =============================================================

# ─────────────────────────────────────────────────────────────
# BEFORE ANYTHING: Prerequisites to install
# ─────────────────────────────────────────────────────────────

# 1. Install AWS CLI
curl "https://awscli.amazonaws.com/awscli-exe-linux-x86_64.zip" -o "awscliv2.zip"
unzip awscliv2.zip && sudo ./aws/install
aws --version   # should print: aws-cli/2.x.x

# 2. Configure AWS credentials (use your IAM user with Admin access for now)
aws configure
# It asks for:
#   AWS Access Key ID:     → paste your key
#   AWS Secret Access Key: → paste your secret
#   Default region:        → us-east-1
#   Default output format: → json

# 3. Install Terraform
wget https://releases.hashicorp.com/terraform/1.7.4/terraform_1.7.4_linux_amd64.zip
unzip terraform_1.7.4_linux_amd64.zip
sudo mv terraform /usr/local/bin/
terraform --version   # should print: Terraform v1.7.4

# 4. Install kubectl
curl -LO "https://dl.k8s.io/release/v1.29.0/bin/linux/amd64/kubectl"
chmod +x kubectl && sudo mv kubectl /usr/local/bin/
kubectl version --client

# 5. Install Helm
curl https://raw.githubusercontent.com/helm/helm/main/scripts/get-helm-3 | bash
helm version

# ─────────────────────────────────────────────────────────────
# STEP 1: Create S3 bucket for Terraform state (DO THIS ONCE)
# Terraform stores what it created in this file — never delete it
# ─────────────────────────────────────────────────────────────

# Replace YOUR-UNIQUE-NAME with something globally unique (e.g. cloudops-tfstate-johndoe-2024)
aws s3api create-bucket \
  --bucket cloudops-agent-terraform-state \
  --region us-east-1

# Enable versioning (lets you recover if state gets corrupted)
aws s3api put-bucket-versioning \
  --bucket cloudops-agent-terraform-state \
  --versioning-configuration Status=Enabled

# Enable encryption
aws s3api put-bucket-encryption \
  --bucket cloudops-agent-terraform-state \
  --server-side-encryption-configuration '{
    "Rules": [{"ApplyServerSideEncryptionByDefault": {"SSEAlgorithm": "AES256"}}]
  }'

# Create DynamoDB table for state locking (prevents two people applying at once)
aws dynamodb create-table \
  --table-name cloudops-terraform-locks \
  --attribute-definitions AttributeName=LockID,AttributeType=S \
  --key-schema AttributeName=LockID,KeyType=HASH \
  --billing-mode PAY_PER_REQUEST \
  --region us-east-1

echo "S3 + DynamoDB ready for Terraform backend ✅"

# ─────────────────────────────────────────────────────────────
# STEP 2: Edit terraform.tfvars
# Open the file and change alert_email to your real email
# ─────────────────────────────────────────────────────────────
# nano infra/terraform.tfvars
# Change: alert_email = "you@youremail.com"

# ─────────────────────────────────────────────────────────────
# STEP 3: Terraform init
# Downloads all providers and modules. Run once per machine.
# ─────────────────────────────────────────────────────────────
cd infra/
terraform init
# Expected output: "Terraform has been successfully initialized!"

# ─────────────────────────────────────────────────────────────
# STEP 4: Terraform plan
# Shows you EXACTLY what will be created — NO changes made yet
# READ THIS OUTPUT before applying. Count the resources.
# ─────────────────────────────────────────────────────────────
terraform plan -out=tfplan
# Expected: ~40-50 resources to add, 0 to change, 0 to destroy
# Main things you'll see: VPC, subnets, EKS cluster, node group,
#   IAM roles, CloudWatch log groups, SNS topic, metric alarms

# ─────────────────────────────────────────────────────────────
# STEP 5: Terraform apply
# THIS CREATES REAL AWS RESOURCES AND COSTS MONEY
# EKS cluster ≈ $0.10/hr, t3.medium nodes ≈ $0.042/hr each
# Total staging cost ≈ $5-8/day
# ─────────────────────────────────────────────────────────────
terraform apply tfplan
# Type "yes" when prompted
# ⏱ Takes 15-20 minutes (EKS cluster creation is slow — normal)
# When done, outputs will show: cluster name, endpoint, role ARN, SNS topic ARN

# ─────────────────────────────────────────────────────────────
# STEP 6: Connect kubectl to your new EKS cluster
# ─────────────────────────────────────────────────────────────
aws eks update-kubeconfig \
  --name cloudops-cluster-staging \
  --region us-east-1

# Verify you can see the cluster nodes
kubectl get nodes
# Expected output (after 2-3 min):
#   NAME                        STATUS   ROLES    AGE   VERSION
#   ip-10-0-1-xxx.ec2.internal  Ready    <none>   2m    v1.29.x
#   ip-10-0-2-xxx.ec2.internal  Ready    <none>   2m    v1.29.x

# ─────────────────────────────────────────────────────────────
# STEP 7: Create the Kubernetes namespace for your agent
# ─────────────────────────────────────────────────────────────
kubectl create namespace cloudops-staging

# Create the service account (this is what IRSA links to the IAM role)
# Replace ROLE_ARN with the value from terraform output agent_role_arn
ROLE_ARN=$(terraform output -raw agent_role_arn)

kubectl create serviceaccount cloudops-agent \
  --namespace cloudops-staging

kubectl annotate serviceaccount cloudops-agent \
  --namespace cloudops-staging \
  eks.amazonaws.com/role-arn=$ROLE_ARN

# Verify the annotation
kubectl describe serviceaccount cloudops-agent -n cloudops-staging
# Should show: Annotations: eks.amazonaws.com/role-arn: arn:aws:iam::...

# ─────────────────────────────────────────────────────────────
# STEP 8: Verify CloudWatch setup
# ─────────────────────────────────────────────────────────────
# Check log groups were created
aws logs describe-log-groups \
  --log-group-name-prefix /cloudops \
  --query 'logGroups[*].logGroupName'

# Check SNS topic exists
aws sns list-topics --query 'Topics[*].TopicArn' | grep cloudops

# Check alarms exist
aws cloudwatch describe-alarms \
  --query 'MetricAlarms[*].{Name:AlarmName,State:StateValue}' \
  --output table

# IMPORTANT: Check your email and click the SNS subscription confirmation link!

# ─────────────────────────────────────────────────────────────
# STEP 9: Test an alarm manually (simulate a trigger)
# ─────────────────────────────────────────────────────────────
# Set an alarm to ALARM state manually to test the SNS → email flow
aws cloudwatch set-alarm-state \
  --alarm-name "cloudops-high-cpu-staging" \
  --state-value ALARM \
  --state-reason "Manual test to verify SNS + email flow"

# Check your email — you should receive a notification within 30 seconds
# Then reset the alarm:
aws cloudwatch set-alarm-state \
  --alarm-name "cloudops-high-cpu-staging" \
  --state-value OK \
  --state-reason "Reset after manual test"

echo "Phase 2 complete! ✅ VPC + EKS + IAM + CloudWatch all deployed."
echo "Next: Phase 3 — build the Pinecone RAG pipeline"

# ─────────────────────────────────────────────────────────────
# CLEANUP (run this when done for the day to avoid charges)
# ─────────────────────────────────────────────────────────────
# terraform destroy   # Deletes EVERYTHING — use only when taking a break
# To just stop nodes: scale down to 0 in the AWS Console → EKS → Node Groups
