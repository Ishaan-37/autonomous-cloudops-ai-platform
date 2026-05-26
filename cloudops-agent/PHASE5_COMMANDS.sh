#!/bin/bash
# ============================================================
# PHASE 5 STEP-BY-STEP COMMANDS
# FASTAPI + SLACK INTEGRATION
# Read every comment before running each command.
# ============================================================


# ─────────────────────────────────────────────────────────────
# YOUR REPO STRUCTURE AFTER PHASE 5
# Copy files into these exact locations:
# ─────────────────────────────────────────────────────────────

# cloudops-agent/
# ├── api/
# │   ├── __init__.py
# │   ├── main.py           <- FastAPI app (all 5 endpoints)
# │   └── slack_setup.py    <- Slack bot verification + test message
# ├── Dockerfile            <- Multi-stage Docker build
# ├── helm/
# │   └── values.yaml       <- Kubernetes deployment config
# └── tests/
#     └── test_api.py       <- 20+ tests for all endpoints


# ─────────────────────────────────────────────────────────────
# STEP 1: Install Phase 5 dependencies
# ─────────────────────────────────────────────────────────────

cd cloudops-agent/
source venv/bin/activate

uv pip install --system \
  fastapi==0.115.0 \
  uvicorn[standard]==0.30.0 \
  httpx==0.27.0 \
  aiohttp==3.10.0 \
  pytest-asyncio==0.24.0

echo "Phase 5 deps installed"


# ─────────────────────────────────────────────────────────────
# STEP 2: Create the Slack App
# DO THIS IN YOUR BROWSER — follow each step exactly
# ─────────────────────────────────────────────────────────────

echo "=== SLACK APP SETUP (do in browser) ==="
echo ""
echo "1. Go to: https://api.slack.com/apps"
echo "2. Click 'Create New App' -> 'From scratch'"
echo "3. Name: CloudOps Agent"
echo "4. Select your workspace -> Create App"
echo ""
echo "5. Left sidebar -> 'OAuth & Permissions'"
echo "   Under 'Bot Token Scopes', add:"
echo "   - chat:write"
echo "   - chat:write.public"
echo "   - channels:read"
echo "   - users:read"
echo ""
echo "6. Scroll up -> 'Install to Workspace' -> Allow"
echo "   COPY the 'Bot User OAuth Token' (starts with xoxb-)"
echo ""
echo "7. Left sidebar -> 'Basic Information'"
echo "   Under 'App Credentials', COPY 'Signing Secret'"
echo ""
echo "8. Left sidebar -> 'Interactivity & Shortcuts'"
echo "   Toggle ON"
echo "   Request URL: (fill in after Step 6 — needs your public URL)"
echo ""
echo "9. In Slack: go to #cloudops-alerts channel"
echo "   Type: /invite @CloudOps-Agent"
echo "   (invites the bot so it can post messages)"


# ─────────────────────────────────────────────────────────────
# STEP 3: Set environment variables
# ─────────────────────────────────────────────────────────────

export OPENAI_API_KEY="sk-your-openai-key"
export PINECONE_API_KEY="pcsk-your-pinecone-key"
export SLACK_BOT_TOKEN="xoxb-your-slack-bot-token"         # from Step 2
export SLACK_SIGNING_SECRET="your-slack-signing-secret"     # from Step 2
export SLACK_ALERT_CHANNEL="#cloudops-alerts"
export AWS_REGION="us-east-1"
export AUDIT_TABLE_NAME="cloudops-audit-log"
export CLOUDOPS_ENVIRONMENT="development"

echo "Env vars set"


# ─────────────────────────────────────────────────────────────
# STEP 4: Create __init__.py for the api package
# ─────────────────────────────────────────────────────────────

touch api/__init__.py
echo "api/__init__.py created"


# ─────────────────────────────────────────────────────────────
# STEP 5: Verify Slack bot is configured correctly
# This sends a test message to #cloudops-alerts with fake buttons
# ─────────────────────────────────────────────────────────────

python api/slack_setup.py

# Expected output:
#   SLACK_BOT_TOKEN:      SET (xoxb-123...)
#   SLACK_SIGNING_SECRET: SET (abc123...)
#   Auth OK - Bot: CloudOps-Agent, Team: Your Workspace
#   Message post OK - channel: #cloudops-alerts
#   Test message sent to #cloudops-alerts
#   (You'll see a message appear in Slack with Approve/Reject buttons)


# ─────────────────────────────────────────────────────────────
# STEP 6: Run the test suite
# ALL 20+ TESTS MUST PASS before testing with real services
# ─────────────────────────────────────────────────────────────

pytest tests/test_api.py -v --tb=short

# Expected output:
#   tests/test_api.py::TestHealthCheck::test_health_returns_200 PASSED
#   tests/test_api.py::TestHealthCheck::test_health_shows_environment PASSED
#   tests/test_api.py::TestCloudWatchWebhook::test_alarm_notification_triggers_agent PASSED
#   tests/test_api.py::TestCloudWatchWebhook::test_ok_state_ignored PASSED
#   tests/test_api.py::TestCloudWatchWebhook::test_invalid_json_returns_400 PASSED
#   tests/test_api.py::TestSlackActions::test_approve_button_resumes_agent PASSED
#   tests/test_api.py::TestSlackActions::test_reject_button_resumes_agent_with_rejected PASSED
#   tests/test_api.py::TestSlackSignatureVerification::test_valid_signature_accepted PASSED
#   tests/test_api.py::TestSlackSignatureVerification::test_invalid_signature_rejected PASSED
#   tests/test_api.py::TestSlackSignatureVerification::test_old_timestamp_rejected PASSED
#   ... (20+ tests)
#   PASSED in X.Xs


# ─────────────────────────────────────────────────────────────
# STEP 7: Start the FastAPI server locally
# ─────────────────────────────────────────────────────────────

uvicorn api.main:app --host 0.0.0.0 --port 8080 --reload

# You should see:
#   INFO: CloudOps Agent API starting (env=development)
#   INFO: CloudOps Agent API ready
#   INFO: Uvicorn running on http://0.0.0.0:8080
#   INFO: Application startup complete.

# Keep this running in terminal 1.
# Open a new terminal for the next steps.


# ─────────────────────────────────────────────────────────────
# STEP 8: Test the endpoints with curl (new terminal)
# ─────────────────────────────────────────────────────────────

# Test 1: Health check
curl http://localhost:8080/health | python3 -m json.tool

# Test 2: Manual alarm trigger (triggers the full agent pipeline)
curl -X POST http://localhost:8080/trigger/test \
  -H "Content-Type: application/json" \
  -d '{"alarm_name":"test-high-cpu","state_reason":"CPU at 92%","instance_id":"i-0abc1234def56789"}' \
  | python3 -m json.tool

# You should see:
#   {"status": "triggered", "run_id": "uuid-here", "check_status": "GET /runs/uuid-here"}
# AND in your Slack channel:
#   The agent's analysis message (if Slack token is real)

# Test 3: Check run status (use run_id from above)
RUN_ID="paste-run-id-from-above-here"
curl http://localhost:8080/runs/$RUN_ID | python3 -m json.tool

# Test 4: Open FastAPI docs in browser
echo "Open: http://localhost:8080/docs"


# ─────────────────────────────────────────────────────────────
# STEP 9: Expose local server to internet with ngrok
# Needed so Slack can call your local webhook for button clicks
# ─────────────────────────────────────────────────────────────

# Install ngrok (one time)
# Mac:   brew install ngrok
# Linux: snap install ngrok
# Or:    Download from https://ngrok.com/download

# Sign up at ngrok.com and get your auth token
ngrok config add-authtoken YOUR_NGROK_TOKEN

# Expose port 8080 to internet
ngrok http 8080

# ngrok will give you a URL like:
#   https://abc123def456.ngrok-free.app

# COPY that URL. You need it for the next step.
# Leave ngrok running in this terminal.


# ─────────────────────────────────────────────────────────────
# STEP 10: Wire ngrok URL into Slack Interactivity
# Now Slack can send button clicks to your local machine
# ─────────────────────────────────────────────────────────────

# 1. Go back to https://api.slack.com/apps -> your app
# 2. Left sidebar -> "Interactivity & Shortcuts"
# 3. Request URL: https://abc123def456.ngrok-free.app/webhook/slack/actions
#    (use YOUR actual ngrok URL)
# 4. Click "Save Changes"

# Now test it:
# In Slack, the test message from Step 5 should still be there
# Click "Approve & Remediate" button
# Watch your terminal — you should see:
#   INFO: Slack action: approve_remediation by yourname run=abc123...
#   INFO: Resuming agent: full-run-id-here


# ─────────────────────────────────────────────────────────────
# STEP 11: Wire ngrok URL into AWS SNS (for real alarm testing)
# ─────────────────────────────────────────────────────────────

# Update your Terraform variable with the ngrok URL
# Edit infra/terraform.tfvars:
#   api_webhook_url = "https://abc123def456.ngrok-free.app/webhook/cloudwatch"

# Then re-apply:
# cd infra/
# terraform apply -var='api_webhook_url=https://abc123def456.ngrok-free.app/webhook/cloudwatch'

# The SNS subscription will be PENDING until you accept the confirmation.
# Check your server logs — you'll see:
#   INFO: SNS message type: SubscriptionConfirmation
#   INFO: Confirming SNS subscription...
#   INFO: SNS subscription confirmed

# Now trigger a real CloudWatch alarm:
aws cloudwatch set-alarm-state \
  --alarm-name "cloudops-high-cpu-staging" \
  --state-value ALARM \
  --state-reason "Test: verifying full pipeline end-to-end" \
  --region us-east-1

# Watch your terminal for:
#   INFO: SNS message type: Notification
#   INFO: Alarm: cloudops-high-cpu-staging state=ALARM
#   INFO: Agent run starting: <uuid>

# Reset the alarm:
aws cloudwatch set-alarm-state \
  --alarm-name "cloudops-high-cpu-staging" \
  --state-value OK \
  --state-reason "Reset after test"


# ─────────────────────────────────────────────────────────────
# STEP 12: Build Docker image
# Test that the container works before deploying to EKS
# ─────────────────────────────────────────────────────────────

# Build the image
docker build -t cloudops-agent:local .

# Run it with your env vars
docker run -p 8080:8080 \
  -e OPENAI_API_KEY=$OPENAI_API_KEY \
  -e PINECONE_API_KEY=$PINECONE_API_KEY \
  -e SLACK_BOT_TOKEN=$SLACK_BOT_TOKEN \
  -e SLACK_SIGNING_SECRET=$SLACK_SIGNING_SECRET \
  -e CLOUDOPS_ENVIRONMENT=development \
  -e AWS_REGION=us-east-1 \
  cloudops-agent:local

# Test it:
curl http://localhost:8080/health

# Expected: {"status":"healthy",...}


# ─────────────────────────────────────────────────────────────
# STEP 13: Push to ECR and deploy to EKS
# ─────────────────────────────────────────────────────────────

# Get your AWS account ID
AWS_ACCOUNT_ID=$(aws sts get-caller-identity --query Account --output text)
ECR_REPO="$AWS_ACCOUNT_ID.dkr.ecr.us-east-1.amazonaws.com/cloudops-agent"

# Create ECR repository (one time)
aws ecr create-repository \
  --repository-name cloudops-agent \
  --region us-east-1

# Login to ECR
aws ecr get-login-password --region us-east-1 | \
  docker login --username AWS --password-stdin $ECR_REPO

# Tag and push
GIT_SHA=$(git rev-parse --short HEAD)
docker tag cloudops-agent:local $ECR_REPO:$GIT_SHA
docker tag cloudops-agent:local $ECR_REPO:latest
docker push $ECR_REPO:$GIT_SHA
docker push $ECR_REPO:latest

echo "Image pushed: $ECR_REPO:$GIT_SHA"

# Update helm/values.yaml:
# image.repository: YOUR_ECR_REPO_URL (paste $ECR_REPO value)

# Deploy to EKS
aws eks update-kubeconfig --name cloudops-cluster-staging --region us-east-1

helm upgrade --install cloudops-agent ./helm \
  --namespace cloudops-staging \
  --create-namespace \
  --set image.repository=$ECR_REPO \
  --set image.tag=$GIT_SHA \
  --set serviceAccount.annotations."eks\.amazonaws\.com/role-arn"=$(terraform -chdir=infra output -raw agent_role_arn) \
  --values helm/values.yaml \
  --wait --timeout=5m

echo "Deployed to EKS"

# Verify pods are running
kubectl get pods -n cloudops-staging
kubectl logs -n cloudops-staging -l app=cloudops-agent --tail=50


# ─────────────────────────────────────────────────────────────
# STEP 14: Get the public URL and update Slack + SNS
# ─────────────────────────────────────────────────────────────

# Get the ALB URL (takes 2-3 min to provision)
kubectl get ingress -n cloudops-staging

# Output:
#   NAME             CLASS  HOSTS                          ADDRESS
#   cloudops-agent   alb    api.cloudops-agent.yourdomain  k8s-cloudops-abc123.us-east-1.elb.amazonaws.com

ALB_URL=$(kubectl get ingress cloudops-agent -n cloudops-staging \
  -o jsonpath='{.status.loadBalancer.ingress[0].hostname}')

echo "ALB URL: http://$ALB_URL"

# Update Slack Interactivity URL:
# https://api.slack.com/apps -> Interactivity -> Request URL:
#   http://$ALB_URL/webhook/slack/actions

# Update SNS subscription with real URL:
# cd infra/
# Edit terraform.tfvars: api_webhook_url = "http://$ALB_URL/webhook/cloudwatch"
# terraform apply


echo ""
echo "=== PHASE 5 COMPLETE ==="
echo ""
echo "What you now have:"
echo "  FastAPI app with 5 endpoints"
echo "  CloudWatch SNS alarm reception"
echo "  Slack approve/reject button handling"
echo "  Slack signature verification (security)"
echo "  Docker container ready for EKS"
echo "  Helm chart for Kubernetes deployment"
echo "  20+ tests passing"
echo ""
echo "FULL PIPELINE NOW WORKS:"
echo "  CloudWatch alarm fires"
echo "  SNS calls /webhook/cloudwatch"
echo "  Agent runs: ingest -> analyze -> plan"
echo "  Slack message with Approve/Reject sent"
echo "  Human clicks Approve"
echo "  Slack calls /webhook/slack/actions"
echo "  Agent resumes: remediate -> report"
echo "  Final report posted to Slack"
echo ""
echo "Next: Phase 6 - FinOps cost optimizer + Grafana observability"
