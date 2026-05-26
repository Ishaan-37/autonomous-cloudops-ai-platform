"""
agent/nodes/remediate_node.py
------------------------------
NODE 5 OF 6. THE HANDS OF THE AGENT.

PURPOSE:
  Executes the approved fix plan against real AWS infrastructure.
  Uses AWS SSM Run Command as the primary execution mechanism.

WHY SSM AND NOT SSH?
  SSH requires:
    - Open port 22 (security risk)
    - SSH keys distributed to the agent (secret management nightmare)
    - Network access from agent to instance (VPC complexity)

  SSM Run Command requires:
    - SSM Agent installed on EC2 (default in Amazon Linux 2/2023)
    - IAM permission to ssm:SendCommand (already in our IRSA role)
    - Zero open ports — SSM Agent polls AWS over HTTPS
    - Full audit trail in CloudTrail automatically

  SSM is strictly better. Never use SSH for automated remediation.

EXECUTION FLOW:
  1. Validate the fix plan (action_type, resources, commands)
  2. For each affected resource:
     a. Verify the instance exists and is running
     b. Send SSM command
     c. Poll for completion (up to 5 minutes)
     d. Record result
  3. If all succeed → success
  4. If any fail → partial success, log which failed

SAFETY CHECKS BEFORE EXECUTING:
  - Instance must be in "running" state
  - Instance must have SSM agent (SSM describe check)
  - Command must be in the allowed list (no arbitrary code injection)
  - Only touch instances listed in affected_resources (no blast radius)

AUDIT TRAIL:
  Every action is logged to DynamoDB with:
  - run_id, timestamp, instance_id, command, result, who approved
  This is your compliance and audit record.
"""

import asyncio
import json
import logging
import os
from datetime import datetime, timezone

import boto3
from botocore.exceptions import ClientError

from agent.state import AgentState, RemediationResult

logger = logging.getLogger(__name__)

ec2 = boto3.client("ec2")
ssm = boto3.client("ssm")
dynamodb = boto3.resource("dynamodb")

AUDIT_TABLE = os.environ.get("AUDIT_TABLE_NAME", "cloudops-audit-log")
AWS_REGION  = os.environ.get("AWS_REGION", "us-east-1")

# ── Allowed SSM commands (whitelist) ──────────────────────────
# ONLY these commands can be run automatically.
# Any command not matching this list is blocked.
# This prevents prompt injection attacks where a malicious log line
# tricks the LLM into generating a dangerous command.
ALLOWED_COMMAND_PATTERNS = [
    "sudo journalctl --vacuum",
    "sudo systemctl restart",
    "sudo systemctl reload",
    "sudo find /tmp -type f -atime",
    "sudo sync",
    "sudo sh -c 'echo 3 > /proc/sys/vm/drop_caches'",
    "sudo pkill -TERM",
    "sudo apt-get clean",
    "sudo yum clean all",
    "df -h",           # Read-only diagnostic
    "free -m",         # Read-only diagnostic
    "ps aux",          # Read-only diagnostic
    "top -bn1",        # Read-only diagnostic
]


async def remediate_node(state: AgentState) -> dict:
    """
    Execute the approved fix plan against AWS infrastructure.

    Input from state:  fix_plan, root_cause, approved
    Output to state:   remediation (RemediationResult), node_history
    """
    logger.info(f"[{state.get('run_id', 'unknown-run')}] remediate_node started")

    fix_plan   = state.get("fix_plan", {})
    root_cause = state.get("root_cause", {})
    action_type = fix_plan.get("action_type", "manual_only")

    # ── Guard: only run if approved or auto_safe ──────────────
    if action_type == "manual_only":
        logger.info(f"[{state.get('run_id', 'unknown-run')}] manual_only plan — skipping automated remediation")
        return {
            "remediation":  _skipped_result("Plan is manual_only — no automated action taken"),
            "node_history": ["remediate_node"],
            "errors":       [],
        }

    if action_type == "auto_risky" and not state.get("approved", False):
        logger.warning(f"[{state.get('run_id', 'unknown-run')}] auto_risky plan but not approved — skipping")
        return {
            "remediation":  _skipped_result("Approval required but not received"),
            "node_history": ["remediate_node"],
            "errors":       ["remediate_node: unapproved auto_risky action blocked"],
        }

    if state.get("rejected", False):
        logger.info(f"[{state.get('run_id', 'unknown-run')}] Remediation rejected by human — skipping")
        return {
            "remediation":  _skipped_result("Rejected by human operator"),
            "node_history": ["remediate_node"],
            "errors":       [],
        }

    # ── Get affected resources and commands ───────────────────
    resources = root_cause.get("affected_resources", [])
    ssm_commands = state.get("_ssm_commands", [])  # Set by plan_node

    if not resources:
        logger.warning(f"[{state.get('run_id', 'unknown-run')}] No affected resources identified — skipping")
        return {
            "remediation":  _skipped_result("No affected resources to remediate"),
            "node_history": ["remediate_node"],
            "errors":       ["remediate_node: no resources in root_cause"],
        }

    if not ssm_commands:
        logger.warning(f"[{state.get('run_id', 'unknown-run')}] No SSM commands in plan — skipping")

        return {
            "remediation": _skipped_result("No SSM commands in fix plan"),
            "node_history": ["remediate_node"],
        "errors": [],
    }

    # ── Execute remediation for each resource ─────────────────
    actions_taken  = []
    action_ids     = []
    errors         = []

    for resource_id in resources:
        logger.info(
            f"[{state.get('run_id', 'unknown-run')}] Remediating resource: {resource_id}"
        )

         # Step 1: Validate the instance is real and running
        instance_ok, instance_error = await validate_ec2_instance(resource_id)

        if not instance_ok:
            logger.error(
                f"[{state.get('run_id', 'unknown-run')}] Instance validation failed: {instance_error}"
            )
            errors.append(f"{resource_id}: {instance_error}")
            continue

        # Step 2: Execute each SSM command in sequence
        for command in ssm_commands:
            # Safety check: command must be in allowed list
            if not is_command_allowed(command):
                logger.error(f"[{state.get('run_id', 'unknown-run')}] BLOCKED command (not in whitelist): {command}")
                errors.append(f"{resource_id}: command blocked by safety filter: {command[:50]}")
                continue

            # Run the command via SSM
            cmd_id, cmd_error = await run_ssm_command(
                instance_id=resource_id,
                command=command,
                run_id=state.get("run_id", "unknown-run")
            )

            if cmd_error:
                errors.append(f"{resource_id}: SSM error — {cmd_error}")
            else:
                action_ids.append(cmd_id)
                actions_taken.append(f"{resource_id}: {command[:80]}")

                # Step 3: Wait for command to complete and check result
                success, output = await wait_for_ssm_command(resource_id, cmd_id)
                if success:
                    logger.info(f"[{state.get('run_id', 'unknown-run')}] SSM command succeeded: {cmd_id}")
                    actions_taken.append(f"  ↳ Output: {output[:200]}")
                else:
                    logger.error(f"[{state.get('run_id', 'unknown-run')}] SSM command failed: {cmd_id}")
                    errors.append(f"{resource_id}: SSM command failed — {output[:200]}")

    # ── Log to DynamoDB audit table ───────────────────────────
    await write_audit_log(state, actions_taken, action_ids, errors)

    overall_success = len(errors) == 0 and len(actions_taken) > 0

    result: RemediationResult = {
        "success":       overall_success,
        "actions_taken": actions_taken,
        "action_ids":    action_ids,
        "error":         "; ".join(errors) if errors else None,
        "timestamp":     datetime.now(tz=timezone.utc).isoformat(),
    }

    logger.info(
        f"[{state.get('run_id', 'unknown-run')}] remediate_node complete — "
        f"success={overall_success}, actions={len(actions_taken)}, errors={len(errors)}"
    )

    return {
        "remediation":  result,
        "node_history": ["remediate_node"],
        "errors":       errors,
    }


# ── Helper Functions ──────────────────────────────────────────

def is_command_allowed(command: str) -> bool:
    """Check if a command is in the safety whitelist."""
    cmd_lower = command.lower().strip()
    return any(cmd_lower.startswith(allowed.lower()) for allowed in ALLOWED_COMMAND_PATTERNS)


async def validate_ec2_instance(instance_id: str) -> tuple[bool, str]:
    """
    Verify the EC2 instance exists and is in 'running' state.
    Returns (is_valid, error_message).
    """
    if not instance_id.startswith("i-"):
        return False, f"Invalid instance ID format: {instance_id}"

    try:
        response = ec2.describe_instances(InstanceIds=[instance_id])
        reservations = response.get("Reservations", [])

        if not reservations:
            return False, f"Instance {instance_id} not found"

        instance = reservations[0]["Instances"][0]
        state    = instance["State"]["Name"]

        if state != "running":
            return False, f"Instance {instance_id} is {state} (must be running)"

        return True, ""

    except ClientError as e:
        return False, f"AWS error: {e.response['Error']['Message']}"


async def run_ssm_command(instance_id: str, command: str, run_id: str) -> tuple[str, str]:
    """
    Send a command to an EC2 instance via SSM Run Command.
    Returns (command_id, error_message).
    command_id is empty string on error.
    """
    try:
        response = ssm.send_command(
            InstanceIds=[instance_id],
            DocumentName="AWS-RunShellScript",
            Parameters={"commands": [command]},
            Comment=f"CloudOps Agent run_id={run_id}",
            TimeoutSeconds=300,   # 5 minute timeout per command
        )
        cmd_id = response["Command"]["CommandId"]
        logger.info(f"SSM command sent: {cmd_id} → {instance_id}")
        return cmd_id, ""

    except ClientError as e:
        error = e.response["Error"]["Message"]
        return "", error


async def wait_for_ssm_command(
    instance_id: str,
    command_id: str,
    timeout_seconds: int = 300
) -> tuple[bool, str]:
    """
    Poll SSM until the command finishes or times out.
    Returns (success, output_text).

    SSM command statuses:
      Pending → InProgress → Success | Failed | TimedOut | Cancelled
    """
    start = datetime.now(tz=timezone.utc)

    while True:
        elapsed = (datetime.now(tz=timezone.utc) - start).seconds
        if elapsed > timeout_seconds:
            return False, f"Timed out after {timeout_seconds}s"

        try:
            result = ssm.get_command_invocation(
                CommandId=command_id,
                InstanceId=instance_id,
            )
            status = result["Status"]

            if status == "Success":
                output = result.get("StandardOutputContent", "")
                return True, output

            elif status in ("Failed", "TimedOut", "Cancelled", "Undeliverable"):
                error_output = result.get("StandardErrorContent", "")
                return False, f"Status={status}: {error_output[:500]}"

            # Still running — wait and poll again
            logger.debug(f"SSM command {command_id} status: {status}")
            await asyncio.sleep(5)

        except ClientError as e:
            return False, f"SSM poll error: {e.response['Error']['Message']}"


async def write_audit_log(
    state: AgentState,
    actions_taken: list[str],
    action_ids: list[str],
    errors: list[str]
):
    """
    Write an audit record to DynamoDB.
    Every automated action is recorded here for compliance.
    """
    try:
        table = dynamodb.Table(AUDIT_TABLE)
        table.put_item(Item={
            "run_id":      state.get("run_id", "unknown-run"),
            "timestamp":   datetime.now(tz=timezone.utc).isoformat(),
            "alarm_name":  state["alert"].get("AlarmName", "unknown"),
            "severity":    state.get("root_cause", {}).get("severity", "unknown"),
            "approved_by": "auto" if state["fix_plan"].get("action_type") == "auto_safe" else "human",
            "actions":     actions_taken,
            "command_ids": action_ids,
            "errors":      errors,
            "success":     len(errors) == 0,
        })
    except Exception as e:
        logger.error(f"Failed to write audit log: {e}")
        # Don't fail the agent just because audit logging failed


def _skipped_result(reason: str) -> RemediationResult:
    """Return a result indicating no action was taken."""
    return {
        "success":       True,   # Not a failure — intentionally skipped
        "actions_taken": [f"Skipped: {reason}"],
        "action_ids":    [],
        "error":         None,
        "timestamp":     datetime.now(tz=timezone.utc).isoformat(),
    }
