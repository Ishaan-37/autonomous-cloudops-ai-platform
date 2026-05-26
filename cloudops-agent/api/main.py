"""
api/main.py
-----------
THE FRONT DOOR OF THE ENTIRE SYSTEM.

This FastAPI app bridges the outside world and your LangGraph agent.

5 ENDPOINTS:
  POST /webhook/cloudwatch    → AWS SNS posts alarm here → triggers agent
  POST /webhook/slack/actions → Slack button clicks → resumes paused agent
  GET  /health                → Kubernetes liveness probe
  GET  /runs/{run_id}         → Check any agent run status
  POST /trigger/test          → Manual test trigger (dev only)

WHY BACKGROUND TASKS?
  SNS requires 200 response within 15 seconds.
  Agent takes 30-120 seconds (LLM calls are slow).
  FastAPI BackgroundTasks: respond 202 immediately, run agent after.

WHY NOT CELERY?
  BackgroundTasks handles <50 alarms/minute fine.
  Upgrade to arq + Redis later if needed.
"""

import hashlib
import hmac
import json
import logging
import os
from dotenv import load_dotenv
import time
import uuid
from contextlib import asynccontextmanager

import boto3
from fastapi import BackgroundTasks, FastAPI, Header, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, PlainTextResponse
from pydantic import BaseModel

from agent.graph import get_run_state, resume_agent, run_agent

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s - %(message)s"
)
logger = logging.getLogger(__name__)

load_dotenv()

SLACK_SIGNING_SECRET = os.environ.get("SLACK_SIGNING_SECRET", "")
ENVIRONMENT          = os.environ.get("CLOUDOPS_ENVIRONMENT", "development")


# ── Startup / Shutdown ────────────────────────────────────────
@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info(f"CloudOps Agent API starting (env={ENVIRONMENT})")
    missing = [v for v in [
        "OPENAI_API_KEY", "PINECONE_API_KEY",
        "SLACK_BOT_TOKEN", "SLACK_SIGNING_SECRET"
    ] if not os.environ.get(v)]

    if missing and ENVIRONMENT == "production":
        raise RuntimeError(f"Missing required env vars: {missing}")
    elif missing:
        logger.warning(f"Missing env vars (ok in dev): {missing}")

    logger.info("CloudOps Agent API ready")
    yield
    logger.info("CloudOps Agent API shutting down")


app = FastAPI(
    title="CloudOps Autonomous AI Platform",
    description="""
# CloudOps Autonomous AI Platform

### Enterprise AI-Powered Incident Response & Cloud Remediation System

---

## Core Capabilities

### AI Incident Intelligence
- Autonomous Root Cause Analysis (RCA)
- GPT-powered failure diagnostics
- RAG-enhanced infrastructure context retrieval
- Multi-stage reasoning workflows

### Cloud Automation
- CloudWatch alarm ingestion
- SNS webhook orchestration
- Slack-based human approvals
- Automated remediation pipelines

### Stateful AI Orchestration
- LangGraph workflow engine
- Persistent checkpointing
- Human-in-the-loop execution
- Multi-node decision routing

---

# Runtime Workflow

```text
CloudWatch Alarm
        ↓
SNS Webhook
        ↓
FastAPI Control Plane
        ↓
LangGraph Execution Engine
        ↓
AI Root Cause Analysis
        ↓
Remediation Planning
        ↓
Slack Approval System
        ↓
Infrastructure Remediation

---

# Platform Architecture

## Infrastructure Layer
- AWS EKS
- Terraform IaC
- IAM + IRSA
- CloudWatch Monitoring

## AI Layer
- OpenAI GPT
- LangChain
- LangGraph
- Pinecone Vector DB

## Backend Layer
- FastAPI
- Async Webhooks
- Slack SDK
- Stateful Execution Engine

---

# Production Features

- Real-time cloud incident ingestion
- AI-powered remediation planning
- Stateful workflow recovery
- Observability-first architecture
- Slack approval workflows
- Secure webhook validation
- Checkpoint-based execution recovery

---

# Engineering Highlights

### Built For
- Cloud Reliability Engineering (SRE)
- AI Operations (AIOps)
- Incident Automation
- Intelligent Infrastructure Management

### Supports
- Human approval gates
- Autonomous remediation
- Failure replay/debugging
- Multi-stage AI reasoning
- Context-aware diagnostics

---

# Author

### Ishaan Maurya
Cloud & AI Infrastructure Engineering

""",
    version="2.0.0",
    lifespan=lifespan,
    docs_url="/docs" if ENVIRONMENT != "production" else None,
    redoc_url="/redoc",
)


app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"] if ENVIRONMENT == "development" else [],
    allow_methods=["GET", "POST"],
    allow_headers=["*"],
)


# ═══════════════════════════════════════════════════════════════
# ENDPOINT 1: Health Check
# ═══════════════════════════════════════════════════════════════

@app.get("/health")
async def health_check():
    """
    Kubernetes liveness + readiness probe.
    Called every 10 seconds by K8s.
    Non-200 = pod gets restarted.
    """
    checks = {
        "status":       "healthy",
        "environment":  ENVIRONMENT,
        "openai_key":   bool(os.environ.get("OPENAI_API_KEY")),
        "pinecone_key": bool(os.environ.get("PINECONE_API_KEY")),
        "slack_token":  bool(os.environ.get("SLACK_BOT_TOKEN")),
    }
    try:
        boto3.client("sts").get_caller_identity()
        checks["aws_connected"] = True
    except Exception:
        checks["aws_connected"] = False

    healthy = all([checks["openai_key"], checks["pinecone_key"], checks["slack_token"]])
    return JSONResponse(content=checks, status_code=200 if healthy else 503)


# ═══════════════════════════════════════════════════════════════
# ENDPOINT 2: CloudWatch / SNS Alarm Webhook
# ═══════════════════════════════════════════════════════════════

@app.post("/webhook/cloudwatch")
async def cloudwatch_webhook(
    request: Request,
    background_tasks: BackgroundTasks,
    x_amz_sns_message_type: str = Header(default="", alias="x-amz-sns-message-type"),
):
    """
    Receives CloudWatch alarms from AWS SNS.

    SNS sends 3 types:
      SubscriptionConfirmation  - sent once, must confirm by visiting URL
      Notification              - actual alarm, triggers the agent
      UnsubscribeConfirmation   - ignore

    DOUBLE JSON PARSE:
      SNS wraps the alarm in an envelope.
      The actual alarm is INSIDE Message as another JSON string.
      You must parse twice.
    """
    body_bytes = await request.body()
    try:
        sns_message = json.loads(body_bytes.decode("utf-8"))
    except json.JSONDecodeError:
        raise HTTPException(status_code=400, detail="Invalid JSON")

    msg_type = sns_message.get("Type", x_amz_sns_message_type)
    logger.info(f"SNS message type: {msg_type}")

    if msg_type == "SubscriptionConfirmation":
        return await _confirm_subscription(sns_message)

    if msg_type == "Notification":
        return await _dispatch_alarm(sns_message, background_tasks)

    return JSONResponse({"status": "ignored", "type": msg_type})


async def _confirm_subscription(sns_message: dict) -> JSONResponse:
    """
    Confirm SNS subscription by visiting SubscribeURL.
    This only happens ONCE when you first wire up the webhook.
    After this, real alarms start flowing.
    """
    import aiohttp
    url = sns_message.get("SubscribeURL")
    if not url:
        raise HTTPException(status_code=400, detail="No SubscribeURL")

    logger.info("Confirming SNS subscription...")
    async with aiohttp.ClientSession() as session:
        async with session.get(url) as resp:
            if resp.status == 200:
                logger.info("SNS subscription confirmed")
                return JSONResponse({"status": "subscription_confirmed"})
            raise HTTPException(status_code=500, detail="Confirmation failed")


async def _dispatch_alarm(sns_message: dict, background_tasks: BackgroundTasks) -> JSONResponse:
    """
    Parse alarm and trigger agent if state is ALARM.
    Ignores OK and INSUFFICIENT_DATA states.
    """
    try:
        alarm = json.loads(sns_message.get("Message", "{}"))
    except json.JSONDecodeError:
        raise HTTPException(status_code=400, detail="Invalid alarm payload")

    alarm_name  = alarm.get("AlarmName", "unknown")
    alarm_state = alarm.get("NewStateValue", "")
    logger.info(f"Alarm: {alarm_name} state={alarm_state}")

    if alarm_state != "ALARM":
        return JSONResponse({"status": "ignored", "state": alarm_state})

    run_id = str(uuid.uuid4())
    background_tasks.add_task(_run_agent_safe, alarm_payload=alarm, run_id=run_id)

    return JSONResponse(
        status_code=202,
        content={"status": "accepted", "run_id": run_id, "alarm": alarm_name}
    )


async def _run_agent_safe(alarm_payload: dict, run_id: str):
    """Safe wrapper for agent background task - never crashes the server."""
    try:
        logger.info(f"Agent run starting: {run_id}")
        await run_agent(alarm_payload, run_id=run_id)
        logger.info(f"Agent run complete: {run_id}")
    except Exception as e:
        logger.error(f"Agent run failed {run_id}: {e}", exc_info=True)


# ═══════════════════════════════════════════════════════════════
# ENDPOINT 3: Slack Button Actions (Approve / Reject)
# ═══════════════════════════════════════════════════════════════

@app.post("/webhook/slack/actions")
async def slack_actions(request: Request, background_tasks: BackgroundTasks):
    """
    Receives Approve/Reject button clicks from Slack.

    FLOW:
      Human clicks button in Slack
        Slack POSTs form data here (within milliseconds)
        We verify Slack signature (security - prevents fakes)
        Extract run_id + decision from button value JSON
        Resume frozen LangGraph graph in background
        Respond to Slack within 3 seconds (Slack requirement)
        Slack updates the message to show who clicked what

    BUTTON VALUE FORMAT (set in approval_node.py):
      {"action": "approve", "run_id": "uuid-here"}
      {"action": "reject",  "run_id": "uuid-here"}
    """
    body_bytes = await request.body()

    if ENVIRONMENT == "production":
        _verify_slack_signature(request.headers, body_bytes)

    form_data   = await request.form()
    raw_payload = form_data.get("payload", "")

    try:
        payload = json.loads(raw_payload)
    except json.JSONDecodeError:
        raise HTTPException(status_code=400, detail="Invalid Slack payload")

    actions   = payload.get("actions", [])
    user_name = payload.get("user", {}).get("name", "unknown")

    if not actions:
        return PlainTextResponse("")

    action    = actions[0]
    action_id = action.get("action_id", "")

    try:
        action_value = json.loads(action.get("value", "{}"))
    except json.JSONDecodeError:
        action_value = {}

    run_id     = action_value.get("run_id", "")
    action_str = action_value.get("action", "")

    logger.info(f"Slack action={action_id} user={user_name} run={run_id[:8]}...")

    if not run_id:
        return PlainTextResponse("Error: missing run_id")

    is_approve = action_id == "approve_remediation" or action_str == "approve"
    is_reject  = action_id == "reject_remediation"  or action_str == "reject"

    if is_approve:
        decision      = {"approved": True,  "rejected": False, "approved_by": user_name}
        response_text = f"*{user_name}* approved - executing remediation now..."
    elif is_reject:
        decision      = {"approved": False, "rejected": True,  "approved_by": user_name}
        response_text = f"*{user_name}* rejected - no automated action taken."
    else:
        logger.warning(f"Unknown action_id: {action_id}")
        return PlainTextResponse("")

    background_tasks.add_task(_resume_agent_safe, run_id=run_id, human_decision=decision)

    return JSONResponse({
        "response_type":    "in_channel",
        "replace_original": True,
        "text":             response_text,
    })


async def _resume_agent_safe(run_id: str, human_decision: dict):
    """Safe wrapper for agent resume - never crashes the server."""
    try:
        logger.info(f"Resuming agent: {run_id}")
        await resume_agent(run_id, human_decision)
        logger.info(f"Agent resumed complete: {run_id}")
    except Exception as e:
        logger.error(f"Resume failed {run_id}: {e}", exc_info=True)


# ═══════════════════════════════════════════════════════════════
# ENDPOINT 4: Run Status Check
# ═══════════════════════════════════════════════════════════════

@app.get("/runs/{run_id}")
async def get_run_status(run_id: str):
    """
    Get the current status of an agent run.
    """

    try:
        # TODO: fetch run state from checkpointer
        return {
            "run_id": run_id,
            "status": "completed"
        }

    except HTTPException:
        raise

    except Exception as e:
        raise HTTPException(
            status_code=500,
            detail=str(e)
        )# ═══════════════════════════════════════════════════════════════
# ENDPOINT 5: Manual Test Trigger
# ═══════════════════════════════════════════════════════════════

class TestTriggerBody(BaseModel):
    alarm_name:   str = "test-high-cpu"
    state_reason: str = "CPU at 95% for 10 minutes"
    instance_id:  str = "i-0abc1234def56789"


@app.post("/trigger/test")
async def test_trigger(body: TestTriggerBody, background_tasks: BackgroundTasks):
    """
    Fire a fake alarm to test the full pipeline end-to-end.
    DISABLED in production.

    Usage:
      curl -X POST http://localhost:8080/trigger/test
        -H "Content-Type: application/json"
        -d '{"alarm_name":"test","state_reason":"CPU at 95%"}'
    """
    if ENVIRONMENT == "production":
        raise HTTPException(status_code=403, detail="Disabled in production")

    run_id = str(uuid.uuid4())
    fake_alarm = {
        "AlarmName":     body.alarm_name,
        "NewStateValue": "ALARM",
        "OldStateValue": "OK",
        "StateReason":   body.state_reason,
        "Region":        "us-east-1",
        "Trigger": {
            "MetricName": "CPUUtilization",
            "Namespace":  "AWS/EC2",
            "Dimensions": [{"name": "InstanceId", "value": body.instance_id}],
            "Threshold":  85.0,
        }
    }

    background_tasks.add_task(_run_agent_safe, alarm_payload=fake_alarm, run_id=run_id)
    return JSONResponse(
        status_code=202,
        content={"status": "triggered", "run_id": run_id,
                 "check_status": f"GET /runs/{run_id}"}
    )


# ═══════════════════════════════════════════════════════════════
# SECURITY
# ═══════════════════════════════════════════════════════════════

def _verify_slack_signature(headers, body: bytes):
    """
    Verify the request came from Slack (not a fake).

    HOW:
      Slack signs every request with your signing secret.
      We recompute the signature from body + timestamp.
      If they match = real Slack. If not = reject.

    REPLAY PROTECTION:
      Timestamp must be within 5 minutes.
      Prevents capturing + replaying old valid requests.
    """
    if not SLACK_SIGNING_SECRET:
        logger.warning("SLACK_SIGNING_SECRET not set - skipping verification")
        return

    sig       = headers.get("x-slack-signature", "")
    timestamp = headers.get("x-slack-request-timestamp", "")

    if not sig or not timestamp:
        raise HTTPException(status_code=401, detail="Missing Slack signature headers")

    if abs(time.time() - float(timestamp)) > 300:
        raise HTTPException(status_code=401, detail="Timestamp too old")

    base     = f"v0:{timestamp}:{body.decode('utf-8')}"
    expected = "v0=" + hmac.new(
        SLACK_SIGNING_SECRET.encode(),
        base.encode(),
        hashlib.sha256
    ).hexdigest()

    if not hmac.compare_digest(expected, sig):
        raise HTTPException(status_code=401, detail="Invalid Slack signature")


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("api.main:app", host="0.0.0.0", port=8080, reload=True)
