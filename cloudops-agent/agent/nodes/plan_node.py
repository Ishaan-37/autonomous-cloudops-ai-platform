from dotenv import load_dotenv
load_dotenv()
"""
agent/nodes/plan_node.py
------------------------
NODE 3 OF 6.

PURPOSE:
  Takes the root cause analysis from analyze_node and asks GPT-4:
  "What is the safest, most effective fix for this issue?"

  Returns a STRUCTURED fix plan with:
  - action_type:  auto_safe | auto_risky | manual_only | no_action
  - steps:        ordered list of remediation steps
  - estimated_risk: low | medium | high
  - requires_approval: True/False
  - rollback_plan: what to do if the fix makes things worse

THE CRITICAL DECISION — action_type:
  auto_safe    → agent acts immediately, no human needed
                 (e.g. restart a stuck service, clear /tmp disk space)

  auto_risky   → agent sends Slack approval request, waits for human
                 (e.g. reboot an EC2 instance, scale up a node group)

  manual_only  → agent sends detailed instructions to Slack
                 (e.g. database corruption, network misconfiguration)
                 Too complex / risky for automation.

  no_action    → alarm is informational, no fix needed
                 (e.g. a spike that already resolved itself)

WHY SEPARATE PLAN FROM ANALYZE?
  Single Responsibility Principle. The analyze node focuses on
  WHAT is wrong. The plan node focuses on HOW to fix it.
  This makes each node easier to test, tune, and swap out.
  You could replace plan_node with a rule-based system for certain
  categories without touching analyze_node at all.

KNOWN SAFE ACTIONS (hardcoded):
  We maintain a list of actions the agent can take WITHOUT human approval.
  This list is intentionally conservative. When in doubt: require approval.
"""

import json
import logging

from openai import AsyncOpenAI
from pydantic import BaseModel, Field, validator

from agent.state import AgentState, FixPlan

logger = logging.getLogger(__name__)
oai = AsyncOpenAI()

# ── Actions that are ALWAYS safe to auto-execute ──────────────
# These are idempotent, reversible, and have minimal blast radius.
ALWAYS_SAFE_ACTIONS = {
    "disk_cleanup":      "sudo journalctl --vacuum-size=500M && sudo apt-get clean",
    "restart_service":   "sudo systemctl restart {service_name}",
    "clear_tmp":         "sudo find /tmp -type f -atime +1 -delete",
    "flush_cache":       "sudo sync && sudo sh -c 'echo 3 > /proc/sys/vm/drop_caches'",
    "kill_zombie_procs": "sudo pkill -TERM -P 1 defunct || true",
}

# ── Actions that REQUIRE human approval before executing ──────
RISKY_ACTIONS = {
    "reboot_instance",
    "scale_out_nodegroup",
    "terminate_instance",
    "increase_instance_type",
    "modify_security_group",
    "delete_old_snapshots",
}

PLAN_SYSTEM_PROMPT = """
You are a Senior Site Reliability Engineer creating a remediation plan.
You have already analyzed the root cause. Now create the SAFEST possible fix.

ACTION TYPE RULES (follow these strictly):
- auto_safe:   Only use if the action is FULLY reversible and risk is LOW.
               Examples: clear disk space, restart a hung process, flush cache.
               NEVER auto_safe for: reboots, instance changes, anything that
               causes even brief service interruption.

- auto_risky:  Use when automated fix is possible but requires human approval.
               Examples: EC2 reboot, scaling operations, service restarts that
               cause brief downtime.

- manual_only: Use when the fix is complex, requires SSH access, involves
               database changes, config file edits, or anything ambiguous.

- no_action:   Use when the alarm is informational or already self-resolved.

RISK LEVELS:
- low:    No user impact, fully reversible in <30 seconds
- medium: Brief service hiccup (<2 min), easily reversible
- high:   Service downtime risk, hard to reverse, data risk

OUTPUT FORMAT:
Respond ONLY with valid JSON. No prose. No markdown.

{
  "summary": "One-line description of the fix",
  "steps": [
    "Step 1: ...",
    "Step 2: ..."
  ],
  "action_type": "auto_safe",
  "estimated_risk": "low",
  "estimated_time": "30 seconds",
  "rollback_plan": "If the fix fails, do X to revert",
  "requires_approval": false,
  "ssm_commands": ["sudo journalctl --vacuum-size=500M"],
  "notes": "Any important context or warnings"
}
""".strip()


class FixPlanOutput(BaseModel):
    summary:          str
    steps:            list[str]
    action_type:      str
    estimated_risk:   str
    estimated_time:   str
    rollback_plan:    str
    requires_approval: bool
    ssm_commands:     list[str] = []
    notes:            str = ""

    @validator("action_type")
    def validate_action_type(cls, v):
        allowed = {"auto_safe", "auto_risky", "manual_only", "no_action"}
        return v if v in allowed else "manual_only"  # Default to safest

    @validator("estimated_risk")
    def validate_risk(cls, v):
        allowed = {"low", "medium", "high"}
        return v.lower() if v.lower() in allowed else "high"  # Default to high when uncertain


async def plan_node(state: AgentState) -> dict:
    """
    Generate a remediation plan based on the root cause analysis.

    Input from state:  root_cause, alert
    Output to state:   fix_plan, node_history
    """
    logger.info(f"[{state['run_id']}] plan_node started")

    rca   = state.get("root_cause", {})
    alert = state["alert"]

    if not rca:
        logger.warning(f"[{state['run_id']}] No root cause available — defaulting to manual_only")
        return {
            "fix_plan":     _manual_only_plan("No root cause analysis available"),
            "node_history": ["plan_node"],
            "errors":       ["plan_node: no root_cause in state"],
        }

    user_message = f"""
ROOT CAUSE ANALYSIS:
{json.dumps(rca, indent=2)}

ORIGINAL ALARM:
Name:   {alert.get('AlarmName')}
Reason: {alert.get('StateReason')}

AVAILABLE SAFE ACTIONS (auto_safe only):
{json.dumps(list(ALWAYS_SAFE_ACTIONS.keys()), indent=2)}

Create the remediation plan. Remember:
- If severity is CRITICAL: prefer manual_only or auto_risky with approval
- If confidence < 0.6: prefer manual_only (uncertain diagnosis = uncertain fix)
- If affected_resources is empty: prefer manual_only (can't target the fix)
- Always include a rollback plan
Output valid JSON only.
""".strip()

    try:
        response = await oai.chat.completions.create(
            model="gpt-4o",
            messages=[
                {"role": "system", "content": PLAN_SYSTEM_PROMPT},
                {"role": "user",   "content": user_message},
            ],
            response_format={"type": "json_object"},
            temperature=0.1,
            max_tokens=800,
            timeout=30,
        )

        raw_json = response.choices[0].message.content
        plan_data = json.loads(raw_json)
        validated = FixPlanOutput(**plan_data)

        # ── Safety override ───────────────────────────────────
        # Even if GPT-4 says auto_safe, double-check against our safe list.
        # If any SSM command looks risky, escalate to auto_risky.
        if validated.action_type == "auto_safe":
            if _commands_look_risky(validated.ssm_commands):
                logger.warning(f"[{state['run_id']}] Safety override: escalating auto_safe → auto_risky")
                validated.action_type    = "auto_risky"
                validated.requires_approval = True

        # Also force approval if severity is CRITICAL
        if rca.get("severity") == "CRITICAL":
            validated.requires_approval = True
            if validated.action_type == "auto_safe":
                validated.action_type = "auto_risky"

        fix_plan: FixPlan = {
            "summary":          validated.summary,
            "steps":            validated.steps,
            "action_type":      validated.action_type,
            "estimated_risk":   validated.estimated_risk,
            "estimated_time":   validated.estimated_time,
            "rollback_plan":    validated.rollback_plan,
            "requires_approval": validated.requires_approval,
        }

        # Store SSM commands separately for the remediate_node
        state["_ssm_commands"] = validated.ssm_commands  # internal field

        logger.info(
            f"[{state['run_id']}] Plan complete — "
            f"action_type={validated.action_type}, "
            f"risk={validated.estimated_risk}, "
            f"approval_needed={validated.requires_approval}"
        )

        return {
            "fix_plan":     fix_plan,
            "node_history": ["plan_node"],
            "errors":       [],
        }

    except Exception as e:
        logger.error(f"[{state['run_id']}] plan_node failed: {e}", exc_info=True)
        return {
            "fix_plan":     _manual_only_plan(str(e)),
            "node_history": ["plan_node"],
            "errors":       [f"plan_node: {e}"],
        }


def _commands_look_risky(commands: list[str]) -> bool:
    """
    Scan SSM commands for patterns that should never run automatically.
    This is a safety net in case GPT-4 hallucinates a risky auto_safe action.
    """
    risky_patterns = [
        "reboot", "shutdown", "poweroff", "init 0",
        "rm -rf", "dd if=", "mkfs", "> /dev/",
        "DROP TABLE", "DELETE FROM", "truncate",
        "iptables -F",  # flushing firewall rules
        "passwd",       # changing passwords
        "chmod 777",    # dangerous permission changes
    ]
    command_text = " ".join(commands).lower()
    return any(pattern.lower() in command_text for pattern in risky_patterns)


def _manual_only_plan(reason: str) -> FixPlan:
    """Fallback plan when planning fails — always safe to fall back to manual."""
    return {
        "summary":           "Manual investigation required",
        "steps":             ["1. Review CloudWatch logs", "2. SSH into affected instance", "3. Investigate manually"],
        "action_type":       "manual_only",
        "estimated_risk":    "high",
        "estimated_time":    "Unknown",
        "rollback_plan":     "No automated actions taken — nothing to roll back",
        "requires_approval": False,
    }
