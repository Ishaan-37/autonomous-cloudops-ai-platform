"""
agent/nodes/approval_node.py
-----------------------------
NODE 4 OF 6. THE HUMAN-IN-THE-LOOP GATE.

PURPOSE:
  When a fix requires human approval (action_type = "auto_risky"),
  this node:
    1. Sends a rich Slack message with the RCA + fix plan + approve/reject buttons
    2. PAUSES the agent graph (LangGraph's interrupt mechanism)
    3. Waits for a human to click Approve or Reject
    4. Resumes the graph with the human's decision

  This is the key safety gate of the entire system.
  No destructive action runs without a human saying yes.

HOW LANGGRAPH INTERRUPT WORKS:
  Normal nodes run and return immediately.
  Interrupt nodes raise a special exception that tells LangGraph:
  "Pause here. Save my state. Resume when you get external input."

  The saved state is stored in a checkpointer (PostgreSQL or SQLite).
  When the Slack button is clicked → FastAPI webhook receives it →
  resumes the graph with the human's choice.

  This means the agent can wait HOURS for approval and still resume
  correctly — it's not blocked in memory waiting.

SLACK MESSAGE FORMAT:
  🚨 [SEVERITY] Alert — CloudOps Agent

  Root Cause: Memory leak in payment-service causing OOM kills
  Confidence: 91% | Category: memory | Resources: i-0abc123

  Proposed Fix: Restart the payment-service via SSM Run Command
  Risk: Low | Estimated Time: 30 seconds
  Steps:
    1. Send SSM command: sudo systemctl restart payment-service
    2. Monitor for 5 minutes
    3. Verify service health check passes

  [✅ Approve & Remediate]  [❌ Reject — I'll fix manually]
"""

import json
import logging
import os

from langgraph.types import interrupt
from slack_sdk.web.async_client import AsyncWebClient

from agent.state import AgentState

logger = logging.getLogger(__name__)
slack = AsyncWebClient(token=os.environ.get("SLACK_BOT_TOKEN", ""))
SLACK_CHANNEL = os.environ.get("SLACK_ALERT_CHANNEL", "#cloudops-alerts")

# ── Severity → emoji + color mapping ─────────────────────────
SEVERITY_CONFIG = {
    "CRITICAL": {"emoji": "🔴", "color": "#FF0000"},
    "HIGH":     {"emoji": "🟠", "color": "#FF8C00"},
    "MEDIUM":   {"emoji": "🟡", "color": "#FFD700"},
    "LOW":      {"emoji": "🟢", "color": "#00C851"},
}


async def approval_node(state: AgentState) -> dict:
    """
    Send Slack approval request and pause the graph until human responds.

    Input from state:  root_cause, fix_plan, run_id
    Output to state:   approved/rejected (set by graph.update_state() externally),
                       approval_msg_ts, node_history
    """
    logger.info(f"[{state['run_id']}] approval_node started")

    fix_plan   = state.get("fix_plan", {})
    root_cause = state.get("root_cause", {})

    # ── Send the Slack message with approve/reject buttons ────
    try:
        msg_ts = await send_approval_message(state, root_cause, fix_plan)
        logger.info(f"[{state['run_id']}] Approval message sent. ts={msg_ts}")
    except Exception as e:
        logger.error(f"[{state['run_id']}] Failed to send Slack approval: {e}")
        msg_ts = None

    # ── PAUSE THE GRAPH HERE ──────────────────────────────────
    # interrupt() raises a special LangGraph exception that:
    #   1. Saves the current state to the checkpointer
    #   2. Returns control to the caller (FastAPI)
    #   3. The graph is "frozen" until graph.update_state() is called
    #
    # When the Slack button is clicked:
    #   FastAPI receives the webhook
    #   Calls graph.update_state(thread_id, {"approved": True})
    #   Then calls graph.invoke(None, config) to RESUME from this point
    #
    # The value passed to interrupt() is available to the caller
    # so they know the graph is waiting for approval.
    human_decision = interrupt({
        "message":   "Waiting for human approval in Slack",
        "run_id":    state["run_id"],
        "msg_ts":    msg_ts,
        "fix_plan":  fix_plan,
    })

    # ── Code below runs AFTER human clicks a button ───────────
    # human_decision is the value passed to graph.update_state()
    approved = human_decision.get("approved", False)
    rejected = human_decision.get("rejected", False)

    if approved:
        logger.info(f"[{state['run_id']}] Human APPROVED remediation")
        await send_slack_reply(msg_ts, "✅ Approved! Starting remediation...")
    else:
        logger.info(f"[{state['run_id']}] Human REJECTED remediation")
        await send_slack_reply(msg_ts, "❌ Rejected. No automated action will be taken.")

    return {
        "approved":         approved,
        "rejected":         rejected,
        "approval_msg_ts":  msg_ts,
        "node_history":     ["approval_node"],
        "errors":           [],
    }


async def send_approval_message(
    state: AgentState,
    rca: dict,
    plan: dict
) -> str:
    """
    Send the Slack Block Kit approval message.
    Returns the message timestamp (ts) for threading follow-ups.
    """
    severity = rca.get("severity", "HIGH")
    cfg      = SEVERITY_CONFIG.get(severity, SEVERITY_CONFIG["HIGH"])
    emoji    = cfg["emoji"]
    color    = cfg["color"]

    # Format the affected resources list
    resources = rca.get("affected_resources", [])
    resources_str = ", ".join(resources) if resources else "Unknown"

    # Format the steps
    steps = plan.get("steps", [])
    steps_str = "\n".join(f"   {i+1}. {s}" for i, s in enumerate(steps))

    # Build Slack Block Kit blocks
    # Block Kit = Slack's rich message format with sections, buttons, dividers
    blocks = [
        # Header
        {
            "type": "header",
            "text": {
                "type": "plain_text",
                "text": f"{emoji} {severity} Alert — CloudOps Agent"
            }
        },
        {"type": "divider"},

        # Root Cause section
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": (
                    f"*🔍 Root Cause*\n{rca.get('root_cause', 'Unknown')}\n\n"
                    f"*Confidence:* {int(rca.get('confidence', 0) * 100)}%  "
                    f"*Category:* `{rca.get('category', 'unknown')}`  "
                    f"*Severity:* `{severity}`"
                )
            }
        },

        # Affected Resources
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": f"*📦 Affected Resources*\n`{resources_str}`"
            }
        },
        {"type": "divider"},

        # Fix Plan section
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": (
                    f"*🔧 Proposed Fix*\n{plan.get('summary', 'No plan available')}\n\n"
                    f"*Risk:* `{plan.get('estimated_risk', 'unknown')}`  "
                    f"*Est. Time:* `{plan.get('estimated_time', 'unknown')}`\n\n"
                    f"*Steps:*\n{steps_str}"
                )
            }
        },

        # Rollback plan
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": f"*🔄 Rollback Plan*\n_{plan.get('rollback_plan', 'N/A')}_"
            }
        },
        {"type": "divider"},

        # Action buttons — the core of this message
        # action_id values are used by the FastAPI webhook to identify what was clicked
        {
            "type": "actions",
            "block_id": f"approval_{state['run_id']}",
            "elements": [
                {
                    "type":      "button",
                    "text":      {"type": "plain_text", "text": "✅ Approve & Remediate"},
                    "style":     "primary",
                    "value":     json.dumps({"action": "approve", "run_id": state["run_id"]}),
                    "action_id": "approve_remediation",
                    "confirm": {
                        "title":   {"type": "plain_text", "text": "Confirm Remediation"},
                        "text":    {"type": "plain_text",
                                    "text": f"This will {plan.get('summary', 'execute the fix')}. Continue?"},
                        "confirm": {"type": "plain_text", "text": "Yes, do it"},
                        "deny":    {"type": "plain_text", "text": "Cancel"}
                    }
                },
                {
                    "type":      "button",
                    "text":      {"type": "plain_text", "text": "❌ Reject — Fix Manually"},
                    "style":     "danger",
                    "value":     json.dumps({"action": "reject", "run_id": state["run_id"]}),
                    "action_id": "reject_remediation",
                },
            ]
        },

        # Footer with run ID for tracing
        {
            "type": "context",
            "elements": [
                {
                    "type": "mrkdwn",
                    "text": f"Run ID: `{state['run_id']}` | Agent: CloudOps AI | Thread this message to add notes"
                }
            ]
        }
    ]

    response = await slack.chat_postMessage(
        channel=SLACK_CHANNEL,
        text=f"{emoji} {severity} Alert — CloudOps Agent requires approval",  # Fallback for notifications
        blocks=blocks,
    )

    return response["ts"]   # Slack message timestamp — used for threading replies


async def send_slack_reply(thread_ts: str, text: str):
    """Send a threaded reply to the approval message."""
    if not thread_ts:
        return
    try:
        await slack.chat_postMessage(
            channel=SLACK_CHANNEL,
            thread_ts=thread_ts,
            text=text
        )
    except Exception as e:
        logger.error(f"Failed to send Slack reply: {e}")
