#!/bin/bash
# ============================================================
# PHASE 6 STEP-BY-STEP COMMANDS
# FINOPS + EKS DEPLOY + OBSERVABILITY
# Read EVERY comment before running each command.
# ============================================================


# ─────────────────────────────────────────────────────────────
# YOUR REPO STRUCTURE AFTER PHASE 6
# ─────────────────────────────────────────────────────────────

# cloudops-agent/
# ├── agent/
# │   └── nodes/
# │       └── cost_optimizer_node.py    ← FinOps nightly analyzer
# ├── observability/
# │   ├── __init__.py
# │   ├── telemetry.py                  ← OTel tracing + metrics
# │   ├── otel_collector.yaml           ← OTel Collector in K8s
# │   └── grafana_dashboard.json        ← Grafana dashboard to import
# ├── helm/
# │   └── agent/
# │       ├── Chart.yaml
# │       ├── values.yaml               ← Updated with all settings
# │       └── templates/
# │           └── deployment.yaml       ← All K8s resources
# ├── argocd/
# │   └── cloudops-agent-app.yaml       ← ArgoCD GitOps config
# └── tests/
#     └── test_phase6.py                ← Phase 6 test suite


# ─────────────────────────────────────────────────────────────
# STEP 1: Install Phase 6 dependencies
# ─────────────────────────────────────────────────────────────

cd cloudops-agent/
source venv/bin/activate

uv pip install --system \
  opentelemetry-sdk==1.27.0 \
  opentelemetry-exporter-otlp==1.27.0 \
  opentelemetry-semantic-conventions==0.48b0 \
  boto3==1.35.0 \
  pyyaml==6.0.2

echo "Phase 6 deps installed"


# ─────────────────────────────────────────────────────────────
# STEP 2: Create package init files
# ─────────────────────────────────────────────────────────────

touch observability/__init__.py
touch agent/nodes/__init__.py    # if not already created in Phase 4

echo "Init files created"


# ─────────────────────────────────────────────────────────────
# STEP 3: Run tests
# ─────────────────────────────────────────────────────────────

pytest tests/test_phase6.py -v --tb=short

# Expected output:
#   TestCostOptimizerDataCollection::test_monthly_spend_by_service PASSED
#   TestCostOptimizerDataCollection::test_monthly_spend_handles_api_error PASSED
#   TestCostOptimizerDataCollection::test_week_over_week_spike_detection PASSED
#   TestCostOptimizerDataCollection::test_no_spike_when_change_small PASSED
#   TestCostOptimizerDataCollection::test_find_idle_instances PASSED
#   TestCostOptimizerDataCollection::test_active_instances_not_flagged PASSED
#   TestCostOptimizerDataCollection::test_find_unattached_volumes PASSED
#   TestCostOptimizerDataCollection::test_find_unused_elastic_ips PASSED
#   TestCostOptimizerLLMAnalysis::test_llm_analysis_success PASSED
#   TestCostOptimizerLLMAnalysis::test_llm_analysis_handles_failure PASSED
#   TestCostOptimizerLLMAnalysis::test_full_cost_optimization_run PASSED
#   TestTelemetry::test_telemetry_imports_without_crash PASSED
#   TestTelemetry::test_traced_decorator_works PASSED
#   TestTelemetry::test_metrics_recording_no_crash PASSED
#   14 passed


# ─────────────────────────────────────────────────────────────
# STEP 4: Run FinOps analyzer locally (with real AWS)
# This costs ~$0.04 in API calls but shows you real savings
# ─────────────────────────────────────────────────────────────

export OPENAI_API_KEY="sk-your-key"
export SLACK_BOT_TOKEN="xoxb-your-token"
export SLACK_FINOPS_CHANNEL="#cloudops-finops"
export AWS_REGION="us-east-1"

python -m agent.nodes.cost_optimizer_node

# Expected output:
#   FinOps analysis starting...
#   Data collected: X idle EC2, X unattached volumes, X unused EIPs
#   Analysis complete: total=$XXX.XX, savings=$XX.XX
#   FinOps report posted to #cloudops-finops
#
# Check your Slack #cloudops-finops channel for the report


# ─────────────────────────────────────────────────────────────
# STEP 5: Create Helm Chart.yaml (required file for Helm)
# ─────────────────────────────────────────────────────────────

mkdir -p helm/agent/templates

cat > helm/agent/Chart.yaml << 'EOF'
apiVersion: v2
name: cloudops-agent
description: Autonomous CloudOps AI Agent
type: application
version: 1.0.0
appVersion: "1.0.0"
EOF

echo "Chart.yaml created"

# Also create values for staging and production
cat > helm/agent/values.staging.yaml << 'EOF'
replicaCount: 1
environment: staging
resources:
  requests:
    cpu: "100m"
    memory: "256Mi"
  limits:
    cpu: "500m"
    memory: "512Mi"
autoscaling:
  minReplicas: 1
  maxReplicas: 3
EOF

cat > helm/agent/values.prod.yaml << 'EOF'
replicaCount: 3
environment: production
resources:
  requests:
    cpu: "250m"
    memory: "512Mi"
  limits:
    cpu: "1000m"
    memory: "1Gi"
autoscaling:
  minReplicas: 3
  maxReplicas: 10
EOF

echo "Environment values files created"


# ─────────────────────────────────────────────────────────────
# STEP 6: Update helm/agent/values.yaml with your real values
# ─────────────────────────────────────────────────────────────

# Get your AWS account ID
AWS_ACCOUNT_ID=$(aws sts get-caller-identity --query Account --output text)
echo "Your AWS Account ID: $AWS_ACCOUNT_ID"

# Get the IAM role ARN from Terraform
cd infra/
AGENT_ROLE_ARN=$(terraform output -raw agent_role_arn)
echo "Agent Role ARN: $AGENT_ROLE_ARN"
cd ..

# Edit helm/agent/values.yaml and replace:
#   YOUR_AWS_ACCOUNT_ID  → your actual account ID
#   YOUR_ACCOUNT_ID      → same account ID in serviceAccount section
echo ""
echo "MANUAL STEP: Edit helm/agent/values.yaml and replace:"
echo "  image.repository: $AWS_ACCOUNT_ID.dkr.ecr.us-east-1.amazonaws.com/cloudops-agent"
echo "  serviceAccount.annotations role-arn: $AGENT_ROLE_ARN"
echo "  ingress.host: your actual domain"


# ─────────────────────────────────────────────────────────────
# STEP 7: Install ArgoCD in EKS
# ─────────────────────────────────────────────────────────────

aws eks update-kubeconfig --name cloudops-cluster-staging --region us-east-1

# Create ArgoCD namespace and install
kubectl create namespace argocd
kubectl apply -n argocd \
  -f https://raw.githubusercontent.com/argoproj/argo-cd/stable/manifests/install.yaml

# Wait for ArgoCD to be ready
kubectl wait --for=condition=available deployment/argocd-server \
  -n argocd --timeout=300s

echo "ArgoCD installed"

# Get the initial admin password
ARGOCD_PASSWORD=$(kubectl -n argocd get secret argocd-initial-admin-secret \
  -o jsonpath="{.data.password}" | base64 -d)
echo "ArgoCD admin password: $ARGOCD_PASSWORD"
echo "SAVE THIS PASSWORD"

# Access ArgoCD UI locally
echo "Run in a new terminal: kubectl port-forward svc/argocd-server -n argocd 8081:443"
echo "Then open: https://localhost:8081 (username: admin)"


# ─────────────────────────────────────────────────────────────
# STEP 8: Connect your Git repo to ArgoCD
# ─────────────────────────────────────────────────────────────

# Install ArgoCD CLI
curl -sSL -o argocd-linux-amd64 \
  https://github.com/argoproj/argo-cd/releases/latest/download/argocd-linux-amd64
chmod +x argocd-linux-amd64 && sudo mv argocd-linux-amd64 /usr/local/bin/argocd

# Login to ArgoCD (in a new terminal after port-forward is running)
argocd login localhost:8081 \
  --username admin \
  --password $ARGOCD_PASSWORD \
  --insecure

# Add your GitHub repo to ArgoCD
# For public repos: no credentials needed
# For private repos: use a GitHub Personal Access Token
argocd repo add https://github.com/YOUR_USERNAME/cloudops-agent.git \
  --username YOUR_GITHUB_USERNAME \
  --password YOUR_GITHUB_TOKEN

echo "Git repo connected to ArgoCD"


# ─────────────────────────────────────────────────────────────
# STEP 9: Update argocd/cloudops-agent-app.yaml with your repo
# Then apply it to deploy your app via GitOps
# ─────────────────────────────────────────────────────────────

# Edit argocd/cloudops-agent-app.yaml:
# Replace: YOUR_USERNAME → your actual GitHub username

# Then apply:
kubectl apply -f argocd/cloudops-agent-app.yaml

# Watch the sync happen in real time
argocd app get cloudops-agent-staging
argocd app sync cloudops-agent-staging

# Watch deployment status
kubectl rollout status deployment/cloudops-agent -n cloudops-staging

echo "App deployed via ArgoCD GitOps"


# ─────────────────────────────────────────────────────────────
# STEP 10: Deploy Observability Stack (OTel + Grafana + Prometheus)
# ─────────────────────────────────────────────────────────────

# Install Prometheus + Grafana using Helm
helm repo add prometheus-community https://prometheus-community.github.io/helm-charts
helm repo add grafana               https://grafana.github.io/helm-charts
helm repo update

# Install kube-prometheus-stack (includes Prometheus + Grafana + Alertmanager)
helm upgrade --install monitoring prometheus-community/kube-prometheus-stack \
  --namespace monitoring \
  --create-namespace \
  --set grafana.adminPassword=your-grafana-password \
  --set prometheus.prometheusSpec.scrapeInterval=30s \
  --wait

echo "Prometheus + Grafana installed"

# Install Grafana Tempo (for distributed tracing)
helm upgrade --install tempo grafana/tempo \
  --namespace monitoring \
  --set tempo.storage.trace.backend=local

# Install Grafana Loki (for logs)
helm upgrade --install loki grafana/loki-stack \
  --namespace monitoring \
  --set grafana.enabled=false  # Grafana already installed above

echo "Tempo + Loki installed"

# Deploy the OTel Collector
kubectl apply -f observability/otel_collector.yaml


# ─────────────────────────────────────────────────────────────
# STEP 11: Access Grafana and import dashboard
# ─────────────────────────────────────────────────────────────

# Port-forward Grafana to your local machine
kubectl port-forward svc/monitoring-grafana -n monitoring 3000:80

# Open: http://localhost:3000
# Username: admin
# Password: your-grafana-password (set in Step 10)

echo "Grafana running at http://localhost:3000"
echo ""
echo "IMPORT THE DASHBOARD:"
echo "1. In Grafana: left sidebar → Dashboards → Import"
echo "2. Click 'Upload JSON file'"
echo "3. Select: observability/grafana_dashboard.json"
echo "4. Click Import"
echo ""
echo "You should see the CloudOps Agent dashboard with:"
echo "  - Alarms processed counter"
echo "  - Remediations executed"
echo "  - Agent run duration histogram"
echo "  - LLM latency"
echo "  - Monthly AWS cost gauge"
echo "  - Cost savings identified"


# ─────────────────────────────────────────────────────────────
# STEP 12: Configure OTel in telemetry.py and integrate into main.py
# ─────────────────────────────────────────────────────────────

# Add this to api/main.py lifespan function (startup):
cat << 'PYTHON'

# In api/main.py lifespan():
from observability.telemetry import setup_telemetry

@asynccontextmanager
async def lifespan(app: FastAPI):
    # Initialize OpenTelemetry (add this line)
    setup_telemetry()
    logger.info("OpenTelemetry initialized")
    # ... rest of lifespan code
    yield

PYTHON

# Add tracing to each agent node (example for analyze_node.py):
cat << 'PYTHON'

# In agent/nodes/analyze_node.py — add @traced decorator:
from observability.telemetry import traced, alarms_processed, llm_calls

@traced("analyze_node")   # This creates a trace span automatically
async def analyze_node(state: AgentState) -> dict:
    # Record the alarm being processed
    alarms_processed.add(1, {
        "severity": state.get("root_cause", {}).get("severity", "unknown"),
        "category": state.get("root_cause", {}).get("category", "unknown"),
    })
    # ... rest of node code
PYTHON


# ─────────────────────────────────────────────────────────────
# STEP 13: Full end-to-end live drill
# Run this to verify the complete system works
# ─────────────────────────────────────────────────────────────

echo "=== FULL SYSTEM DRILL ==="
echo ""
echo "1. Trigger a real CloudWatch alarm:"
aws cloudwatch set-alarm-state \
  --alarm-name "cloudops-high-cpu-staging" \
  --state-value ALARM \
  --state-reason "Phase 6 final drill test" \
  --region us-east-1

echo ""
echo "2. Watch pod logs (in new terminal):"
echo "   kubectl logs -n cloudops-staging -l app=cloudops-agent -f"

echo ""
echo "3. Watch in Grafana:"
echo "   http://localhost:3000 → CloudOps Agent dashboard"
echo "   You should see alarms_processed counter increment"
echo "   And agent run duration appear in the histogram"

echo ""
echo "4. Check Slack:"
echo "   #cloudops-alerts for the agent analysis + approval request"
echo "   Click Approve to trigger remediation"
echo "   Watch #cloudops-alerts for the final report"

echo ""
echo "5. Check ArgoCD:"
echo "   https://localhost:8081 → cloudops-agent-staging app"
echo "   Should show 'Healthy' and 'Synced'"

echo ""
echo "6. Reset the alarm:"
aws cloudwatch set-alarm-state \
  --alarm-name "cloudops-high-cpu-staging" \
  --state-value OK \
  --state-reason "Drill complete" \
  --region us-east-1

echo ""
echo "=== PHASE 6 COMPLETE ==="
echo ""
echo "YOUR FULL SYSTEM NOW HAS:"
echo "  Phase 1: Project scaffold, CI/CD pipeline"
echo "  Phase 2: AWS VPC + EKS + IAM + CloudWatch"
echo "  Phase 3: RAG pipeline (Pinecone + embeddings)"
echo "  Phase 4: LangGraph agent (6 nodes)"
echo "  Phase 5: FastAPI webhooks + Slack integration"
echo "  Phase 6: FinOps optimizer + ArgoCD + Grafana"
echo ""
echo "WHAT THIS SYSTEM DOES AUTOMATICALLY:"
echo "  1. CloudWatch alarm fires"
echo "  2. SNS calls FastAPI webhook"
echo "  3. Agent fetches logs + RAG context"
echo "  4. GPT-4 identifies root cause"
echo "  5. GPT-4 generates fix plan"
echo "  6. Safe fixes: auto-executed immediately"
echo "  7. Risky fixes: Slack approval required"
echo "  8. Human clicks Approve"
echo "  9. SSM executes fix on EC2"
echo " 10. Final report posted to Slack"
echo " 11. Every Monday: FinOps report with savings"
echo " 12. Grafana shows everything in real time"
echo ""
echo "THIS IS AN ELITE-LEVEL PROJECT."
