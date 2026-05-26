"""
agent/state.py
--------------
THE BACKBONE OF THE ENTIRE AGENT.

In LangGraph, every node reads from and writes to a shared "State".
Think of State as the agent's memory — it carries all information
from the first node all the way to the last node.

HOW STATE FLOWS:
  CloudWatch Alarm arrives
       ↓
  State = {alert: {...}, everything else: empty}
       ↓
  ingest_node runs → adds logs to state
       ↓
  State = {alert: {...}, logs: [...], ...}
       ↓
  analyze_node runs → adds root_cause to state
       ↓
  State = {alert: {...}, logs: [...], root_cause: {...}, ...}
       ↓
  ... and so on until report_node at the end

WHY TypedDict?
  Python type hints catch bugs early.
  LangGraph uses the type annotations to validate state updates.
  If a node returns {"rrot_cause": ...} (typo), it fails immediately.

WHY Annotated with operator.add?
  Some fields like `logs` get APPENDED across multiple nodes.
  operator.add means: new value is added to existing list, not replaced.
  Without it, each node would overwrite the previous node's data.
"""

import operator
from datetime import datetime
from typing import Annotated, Any, Optional
from typing_extensions import TypedDict


class RootCauseAnalysis(TypedDict):
    """Structured output from the analyze_node LLM call."""
    root_cause:         str        # Human-readable explanation
    confidence:         float      # 0.0 to 1.0 — how sure the LLM is
    affected_resources: list[str]  # EC2 instance IDs, pod names, etc.
    severity:           str        # CRITICAL | HIGH | MEDIUM | LOW
    category:           str        # memory | cpu | disk | network | application | cost
    symptoms:           list[str]  # observed symptoms from logs
    contributing_factors: list[str]


class FixPlan(TypedDict):
    """Structured output from the plan_node LLM call."""
    summary:         str         # One-line description of the fix
    steps:           list[str]   # Ordered list of remediation steps
    action_type:     str         # auto_safe | auto_risky | manual_only | no_action
    estimated_risk:  str         # low | medium | high
    estimated_time:  str         # "30 seconds" | "5 minutes" etc.
    rollback_plan:   str         # What to do if the fix makes things worse
    requires_approval: bool      # True = needs human to click approve in Slack


class RemediationResult(TypedDict):
    """Output from the remediate_node after actions are taken."""
    success:        bool
    actions_taken:  list[str]    # What was actually done
    action_ids:     list[str]    # SSM command IDs, etc. for tracking
    error:          Optional[str]
    timestamp:      str


class AgentState(TypedDict):
    """
    The complete state object passed between all nodes.

    FIELDS EXPLAINED:
      alert         — the raw CloudWatch alarm payload (input)
      run_id        — unique ID for this agent run (for tracing)
      started_at    — when this run began

      logs          — retrieved CloudWatch log chunks (from ingest_node)
      log_context   — formatted string of logs for LLM (from ingest_node)
      doc_context   — formatted string of AWS docs for LLM (from ingest_node)

      root_cause    — structured RCA output (from analyze_node)
      fix_plan      — structured fix plan (from plan_node)

      approved      — True if human clicked Approve in Slack
      approval_msg  — the Slack message ts for tracking
      rejected      — True if human clicked Reject in Slack

      remediation   — result of running the fix (from remediate_node)

      report        — final human-readable report (from report_node)
      slack_ts      — Slack message timestamp for threading replies

      errors        — Annotated list: errors accumulate across nodes
      node_history  — Annotated list: which nodes ran (for debugging)
    """

    # ── Input ─────────────────────────────────────────────────
    alert:       dict[str, Any]   # Raw SNS/CloudWatch alarm payload
    run_id:      str              # UUID for this specific agent run
    started_at:  str              # ISO timestamp

    # ── Ingestion outputs ─────────────────────────────────────
    logs:        list[dict]       # Raw log chunks from CloudWatch
    log_context: str              # Formatted logs for LLM prompt
    doc_context: str              # Formatted AWS docs for LLM prompt

    # ── Analysis outputs ──────────────────────────────────────
    root_cause:  Optional[RootCauseAnalysis]

    # ── Planning outputs ──────────────────────────────────────
    fix_plan:    Optional[FixPlan]

    # ── Approval state ────────────────────────────────────────
    approved:    bool
    rejected:    bool
    approval_msg_ts: Optional[str]   # Slack message TS for the approval request

    # ── Remediation outputs ───────────────────────────────────
    remediation: Optional[RemediationResult]

    # ── Reporting ─────────────────────────────────────────────
    report:      Optional[str]    # Final markdown report
    slack_ts:    Optional[str]    # Slack message timestamp

    # ── Tracking (these ACCUMULATE across nodes using operator.add) ──
    errors:       Annotated[list[str], operator.add]
    node_history: Annotated[list[str], operator.add]


def initial_state(alert: dict, run_id: str) -> AgentState:
    """
    Create a fresh AgentState for a new agent run.
    Called by the FastAPI webhook when an alarm arrives.

    Usage:
        state = initial_state(alarm_payload, str(uuid.uuid4()))
        result = await agent.ainvoke(state)
    """
    return AgentState(
        alert        = alert,
        run_id       = run_id,
        started_at   = datetime.utcnow().isoformat(),

        logs         = [],
        log_context  = "",
        doc_context  = "",

        root_cause   = None,
        fix_plan     = None,

        approved     = False,
        rejected     = False,
        approval_msg_ts = None,

        remediation  = None,
        report       = None,
        slack_ts     = None,

        errors       = [],
        node_history = [],
    )
