"""
agent/nodes/report_node.py
---------------------------
NODE 6 OF 6. THE VOICE OF THE AGENT.

PURPOSE:
  The last node in every graph run. Sends a final comprehensive report
  to Slack (and optionally Discord) summarizing:
  - What alarm fired
  - What the root cause was
  - What fix was proposed
  - What was actually done (or why nothing was done)
  - How long the entire run took
  - What to watch for next

THIS NODE ALWAYS RUNS — even if earlier nodes failed.
  If analyze_node crashed → report_node says "analysis failed, needs manual review"
  If remediation was rejected → report_node says "fix rejected, awaiting manual fix"
  If everything succeeded → report_node says "✅ fixed automatically in 45 seconds"

  The on-call engineer always gets a complete picture, no matter what happened.

REPORT TYPES:
  ✅ SUCCESS    — alarm detected, root cause found, fix auto-applied
  👤 MANUAL     — alarm detected, root cause found, manual fix needed
  ❌ FAILED     — something went wrong in the agent itself
  ℹ️ INFO       — informational alarm, no action needed
  🚫 REJECTED   — fix was proposed but human said no
"""

import logging
import os
from datetime import datetime, timezone

from slack_sdk.web.async_client import AsyncWebClient

from agent.state import AgentState

logger = logging.getLogger(__name__)
slack = AsyncWebClient(token=os.environ.get("SLACK_BOT_TOKEN", ""))
SLACK_CHANNEL = os.environ.get("SLACK_ALERT_CHANNEL", "#cloudops-alerts")


async def report_node(state: AgentState) -> dict:
    """
    Send final report to Slack. Always runs regardless of earlier node outcomes.

    Input from state:  everything accumulated across all nodes
    Output to state:   report (str), slack_ts, node_history
    """
    logger.info(f"[{state.get('run_id', 'unknown-run')}] report_node started")

    # ── Determine overall outcome ─────────────────────────────
    outcome = determine_outcome(state)
    report_text = build_report_text(state, outcome)

    # ── Send to Slack ─────────────────────────────────────────
    slack_ts = await send_slack_report(state, outcome, report_text)

    # ── Log the final summary ─────────────────────────────────
    elapsed = calculate_elapsed(state.get("started_at"))
    logger.info(
        f"[{state.get('run_id', 'unknown-run')}] Agent run COMPLETE — "
        f"outcome={outcome}, "
        f"elapsed={elapsed}s, "
        f"nodes={state.get('node_history', [])}"
    )

    return {
        "report":       report_text,
        "slack_ts":     slack_ts,
        "node_history": ["report_node"],
        "errors":       [],
    }


def determine_outcome(state: AgentState) -> str:
    """Figure out what happened this run."""
    fix_plan    = state.get("fix_plan", {})
    remediation = state.get("remediation", {})
    errors      = state.get("errors", [])
    rejected    = state.get("rejected", False)

    action_type = fix_plan.get("action_type", "manual_only") if fix_plan else None
    rem_success = remediation.get("success", False) if remediation else False

    if rejected:
        return "REJECTED"
    elif action_type == "no_action":
        return "INFO"
    elif action_type == "manual_only":
        return "MANUAL"
    elif rem_success:
        return "SUCCESS"
    elif errors and not remediation:
        return "FAILED"
    else:
        return "PARTIAL"


def build_report_text(state: AgentState, outcome: str) -> str:
    """Build the full markdown report text stored in state."""
    rca         = state.get("root_cause", {})
    fix_plan    = state.get("fix_plan", {})
    remediation = state.get("remediation", {})
    errors      = state.get("errors", [])
    elapsed     = calculate_elapsed(state.get("started_at"))

    lines = [
        f"# CloudOps Agent Report",
        f"**Run ID:** {state.get('run_id', 'unknown-run')}"
        f"**Alarm:** {state.get('alert', {}).get('AlarmName', 'Unknown')}",
        f"**Outcome:** {outcome}",
        f"**Total Time:** {elapsed}s",
        "",
        "## Root Cause Analysis",
        rca.get("root_cause", "No analysis performed") if rca else "Analysis failed",
        "",
        f"**Severity:** {rca.get('severity', 'N/A') if rca else 'N/A'}  "
        f"**Confidence:** {int(rca.get('confidence', 0) * 100) if rca else 0}%  "
        f"**Category:** {rca.get('category', 'N/A') if rca else 'N/A'}",
    ]

    if fix_plan:
        lines += [
            "",
            "## Fix Plan",
            fix_plan.get("summary", "N/A"),
            f"**Action Type:** {fix_plan.get('action_type', 'N/A')}  "
            f"**Risk:** {fix_plan.get('estimated_risk', 'N/A')}",
        ]

    if remediation:
        actions = remediation.get("actions_taken", [])
        lines += [
            "",
            "## Remediation Actions",
            "\n".join(f"- {a}" for a in actions) or "No actions taken",
        ]

    if errors:
        lines += ["", "## Errors", "\n".join(f"- {e}" for e in errors)]

    return "\n".join(lines)


async def send_slack_report(state: AgentState, outcome: str, report_text: str) -> str:
    """Send the final report as a Slack message. Returns message ts."""

    rca         = state.get("root_cause", {})
    fix_plan    = state.get("fix_plan", {})
    remediation = state.get("remediation", {})
    errors      = state.get("errors", [])
    elapsed     = calculate_elapsed(state.get("started_at"))

    # Outcome → emoji + header text
    outcome_config = {
        "SUCCESS":  ("✅", "Issue Auto-Remediated", "#00C851"),
        "MANUAL":   ("👤", "Manual Action Required", "#FFD700"),
        "REJECTED": ("🚫", "Remediation Rejected",   "#FF8C00"),
        "INFO":     ("ℹ️",  "Informational — No Action Needed", "#4A90D9"),
        "FAILED":   ("❌", "Agent Error — Manual Review Needed", "#FF0000"),
        "PARTIAL":  ("⚠️", "Partial Remediation — Check Logs",  "#FF8C00"),
    }
    emoji, header, color = outcome_config.get(outcome, ("❓", "Unknown Outcome", "#888888"))

    # Build the Slack blocks
    blocks = [
        {
            "type": "header",
            "text": {"type": "plain_text", "text": f"{emoji} {header}"}
        },
        {"type": "divider"},
        {
            "type": "section",
            "fields": [
                {"type": "mrkdwn", "text": f"*Alarm*\n{state.get('alert', {}).get('AlarmName', 'Unknown')}"},
                {"type": "mrkdwn", "text": f"*Duration*\n{elapsed}s"},
                {"type": "mrkdwn", "text": f"*Severity*\n`{rca.get('severity', 'N/A') if rca else 'N/A'}`"},
                {"type": "mrkdwn", "text": f"*Confidence*\n{int(rca.get('confidence', 0)*100) if rca else 0}%"},
            ]
        },
    ]

    # Root cause summary
    if rca:
        blocks.append({
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": f"*🔍 Root Cause*\n{rca.get('root_cause', 'N/A')[:500]}"
            }
        })

    # Actions taken
    if remediation:
        actions = remediation.get("actions_taken", [])
        actions_text = "\n".join(f"• {a}" for a in actions[:5]) or "No actions taken"
        blocks.append({
            "type": "section",
            "text": {"type": "mrkdwn", "text": f"*🔧 Actions Taken*\n{actions_text}"}
        })

    # Manual steps (if manual_only)
    if fix_plan and fix_plan.get("action_type") == "manual_only":
        steps = fix_plan.get("steps", [])
        steps_text = "\n".join(f"{i+1}. {s}" for i, s in enumerate(steps[:5]))
        blocks.append({
            "type": "section",
            "text": {"type": "mrkdwn",
                     "text": f"*📋 Manual Steps Required*\n{steps_text}"}
        })

    # Error summary
    if errors and outcome in ("FAILED", "PARTIAL"):
        error_text = "\n".join(f"• {e}" for e in errors[:3])
        blocks.append({
            "type": "section",
            "text": {"type": "mrkdwn", "text": f"*⚠️ Errors*\n{error_text}"}
        })

    # Footer
    node_path = " → ".join(state.get("node_history", []))
    blocks.append({
        "type": "context",
        "elements": [
            {"type": "mrkdwn",
             "text": f"Run ID: `{state.get('run_id', 'unknown-run')}` | Nodes: `{node_path}`"}
        ]
    })

    # Thread into the approval message if one exists
    kwargs = {"channel": SLACK_CHANNEL, "blocks": blocks,
              "text": f"{emoji} {header} — {state.get('alert', {}).get('AlarmName', '')}"}
    if state.get("approval_msg_ts"):
        kwargs["thread_ts"] = state["approval_msg_ts"]

    try:
        response = await slack.chat_postMessage(**kwargs)
        return response["ts"]
    except Exception as e:
        logger.error(f"[{state.get('run_id', 'unknown-run')}] Failed to send Slack report: {e}")
        return ""


def calculate_elapsed(started_at: str) -> int:
    """Calculate seconds since the agent run started."""
    if not started_at:
        return 0
    try:
        start = datetime.fromisoformat(started_at.replace("Z", "+00:00"))
        return int((datetime.now(tz=timezone.utc) - start).total_seconds())
    except Exception:
        return 0
