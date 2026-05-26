"""
api/slack_setup.py
------------------
SLACK BOT CONFIGURATION GUIDE + HELPER UTILITIES.

RUN THIS FILE ONCE to verify your Slack bot is configured correctly:
  python api/slack_setup.py

WHAT YOUR SLACK BOT NEEDS:
  OAuth Scopes (Bot Token Scopes):
    chat:write        - post messages to channels
    chat:write.public - post to channels the bot isn't in
    channels:read     - list channels
    users:read        - get user info (for approval attribution)

  Event Subscriptions: NOT needed (we use webhooks, not events)

  Interactivity:
    Enable: YES
    Request URL: https://your-api-domain.com/webhook/slack/actions

  Install to Workspace: YES (generates the Bot Token xoxb-...)

STEP BY STEP TO CREATE THE SLACK APP:
  1. Go to https://api.slack.com/apps
  2. Click "Create New App" -> "From scratch"
  3. Name: "CloudOps Agent"
  4. Pick your workspace
  5. Go to "OAuth & Permissions" -> add the scopes above
  6. Go to "Interactivity & Shortcuts" -> enable -> add your URL
  7. Go to "Install App" -> Install to Workspace
  8. Copy "Bot User OAuth Token" (xoxb-...)
  9. Go to "Basic Information" -> copy "Signing Secret"
"""

import asyncio
import logging
import os
from dotenv import load_dotenv

from slack_sdk.web.async_client import AsyncWebClient
from slack_sdk.errors import SlackApiError

logger = logging.getLogger(__name__)
load_dotenv()

async def verify_slack_connection() -> dict:
    """
    Test that your Slack bot token is valid and has correct permissions.
    Run this before deploying to catch configuration issues early.
    """
    token = os.environ.get("SLACK_BOT_TOKEN", "")
    if not token:
        return {"ok": False, "error": "SLACK_BOT_TOKEN not set"}

    client = AsyncWebClient(token=token)

    results = {}

    # Test 1: Can we authenticate?
    try:
        auth = await client.auth_test()
        results["auth"] = {
            "ok":       True,
            "bot_name": auth.get("bot_id"),
            "team":     auth.get("team"),
            "user":     auth.get("user"),
        }
        print(f"Auth OK - Bot: {auth.get('user')}, Team: {auth.get('team')}")
    except SlackApiError as e:
        results["auth"] = {"ok": False, "error": str(e)}
        print(f"Auth FAILED: {e}")
        return results

    # Test 2: Can we post a message?
    channel = os.environ.get("SLACK_ALERT_CHANNEL", "#cloudops-alerts")
    try:
        msg = await client.chat_postMessage(
            channel=channel,
            text="CloudOps Agent: Slack connection test - ignore this message",
        )
        results["post_message"] = {"ok": True, "ts": msg.get("ts")}
        print(f"Message post OK - channel: {channel}")

        # Clean up the test message
        await client.chat_delete(channel=msg["channel"], ts=msg["ts"])
    except SlackApiError as e:
        results["post_message"] = {"ok": False, "error": str(e)}
        print(f"Message post FAILED: {e}")
        print(f"  Make sure the bot is invited to {channel}")
        print(f"  In Slack: /invite @CloudOps-Agent in {channel}")

    return results


async def send_test_approval_message() -> str:
    """
    Send a test approval message to Slack with real buttons.
    Use this to verify the Block Kit UI looks correct.

    Returns the message timestamp for cleanup.
    """
    token   = os.environ.get("SLACK_BOT_TOKEN", "")
    channel = os.environ.get("SLACK_ALERT_CHANNEL", "#cloudops-alerts")
    client  = AsyncWebClient(token=token)

    import json
    test_run_id = "test-run-12345678"

    blocks = [
        {
            "type": "header",
            "text": {"type": "plain_text", "text": "TEST - CloudOps Agent Approval Request"}
        },
        {"type": "divider"},
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": (
                    "*Root Cause*\nThis is a TEST message. "
                    "In a real alert, this shows the root cause analysis.\n\n"
                    "*Confidence:* 91%  *Category:* `cpu`  *Severity:* `HIGH`"
                )
            }
        },
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": (
                    "*Proposed Fix*\nRestart the app service via SSM Run Command\n"
                    "*Risk:* `low`  *Est. Time:* `30 seconds`"
                )
            }
        },
        {"type": "divider"},
        {
            "type": "actions",
            "block_id": f"approval_{test_run_id}",
            "elements": [
                {
                    "type":      "button",
                    "text":      {"type": "plain_text", "text": "Approve & Remediate"},
                    "style":     "primary",
                    "value":     json.dumps({"action": "approve", "run_id": test_run_id}),
                    "action_id": "approve_remediation",
                },
                {
                    "type":      "button",
                    "text":      {"type": "plain_text", "text": "Reject - Fix Manually"},
                    "style":     "danger",
                    "value":     json.dumps({"action": "reject", "run_id": test_run_id}),
                    "action_id": "reject_remediation",
                }
            ]
        },
        {
            "type": "context",
            "elements": [{"type": "mrkdwn", "text": f"TEST | Run ID: `{test_run_id}`"}]
        }
    ]

    response = await client.chat_postMessage(
        channel=channel,
        text="TEST - CloudOps Agent Approval Request",
        blocks=blocks,
    )

    print(f"Test message sent to {channel}")
    print(f"Message TS: {response['ts']}")
    print("Click the buttons to verify Slack interactivity is wired correctly")
    print("(Button clicks will fail with 'operation failed' until your API is deployed)")
    return response["ts"]


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)

    async def main():
        print("=== Slack Bot Verification ===\n")

        # Check token is set
        token = os.environ.get("SLACK_BOT_TOKEN", "")
        signing = os.environ.get("SLACK_SIGNING_SECRET", "")
        channel = os.environ.get("SLACK_ALERT_CHANNEL", "#cloudops-alerts")

        print(f"SLACK_BOT_TOKEN:      {'SET (' + token[:12] + '...)' if token else 'NOT SET'}")
        print(f"SLACK_SIGNING_SECRET: {'SET (' + signing[:8] + '...)' if signing else 'NOT SET'}")
        print(f"SLACK_ALERT_CHANNEL:  {channel}\n")

        if not token:
            print("ERROR: Set SLACK_BOT_TOKEN before running this script")
            return

        results = await verify_slack_connection()

        print("\n=== Results ===")
        for check, result in results.items():
            status = "OK" if result.get("ok") else "FAILED"
            print(f"  {check}: {status}")
            if not result.get("ok"):
                print(f"    Error: {result.get('error')}")

        if all(r.get("ok") for r in results.values()):
            print("\nAll checks passed! Sending test approval message...")
            await send_test_approval_message()
        else:
            print("\nFix the errors above before proceeding")

    asyncio.run(main())
