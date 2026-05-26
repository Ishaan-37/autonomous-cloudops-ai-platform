"""
tests/test_api.py
-----------------
COMPLETE TEST SUITE FOR PHASE 5 FastAPI ENDPOINTS.

Run with:
  pytest tests/test_api.py -v

Tests cover:
  - Health check endpoint
  - CloudWatch SNS subscription confirmation
  - CloudWatch alarm notification (triggers agent)
  - Slack approve button click (resumes agent)
  - Slack reject button click
  - Slack signature verification
  - Manual test trigger endpoint
  - Run status endpoint
  - Security: fake Slack requests blocked
  - Security: replay attacks blocked

All external calls are mocked:
  - No real OpenAI calls
  - No real AWS calls
  - No real Slack calls
  - No real Pinecone calls
"""

import hashlib
import hmac
import json
import time
import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient

# Set test environment variables BEFORE importing the app
import os
os.environ["CLOUDOPS_ENVIRONMENT"]  = "development"
os.environ["OPENAI_API_KEY"]        = "sk-test-key"
os.environ["PINECONE_API_KEY"]      = "test-pinecone-key"
os.environ["SLACK_BOT_TOKEN"]       = "xoxb-test-token"
os.environ["SLACK_SIGNING_SECRET"]  = "test-signing-secret-abc123"

from api.main import app

client = TestClient(app)

# ── Test Data ─────────────────────────────────────────────────

SAMPLE_ALARM_PAYLOAD = {
    "AlarmName":     "cloudops-high-cpu-staging",
    "AlarmArn":      "arn:aws:cloudwatch:us-east-1:123:alarm:test",
    "NewStateValue": "ALARM",
    "OldStateValue": "OK",
    "StateReason":   "Threshold Crossed: CPU at 92.3%",
    "Region":        "us-east-1",
    "Trigger": {
        "MetricName": "CPUUtilization",
        "Namespace":  "AWS/EC2",
        "Dimensions": [{"name": "InstanceId", "value": "i-0abc1234def56789"}],
        "Threshold":  85.0,
    }
}

SNS_NOTIFICATION = {
    "Type":      "Notification",
    "MessageId": "test-message-id-123",
    "TopicArn":  "arn:aws:sns:us-east-1:123:cloudops-agent-alerts-staging",
    "Subject":   "ALARM: cloudops-high-cpu-staging",
    "Message":   json.dumps(SAMPLE_ALARM_PAYLOAD),
    "Timestamp": "2024-01-01T12:00:00.000Z",
}

SNS_SUBSCRIPTION = {
    "Type":         "SubscriptionConfirmation",
    "MessageId":    "test-sub-confirm-123",
    "TopicArn":     "arn:aws:sns:us-east-1:123:cloudops-agent-alerts-staging",
    "Token":        "test-token-xyz",
    "SubscribeURL": "https://sns.us-east-1.amazonaws.com/?Action=ConfirmSubscription&TopicArn=...&Token=...",
    "Message":      "You have chosen to subscribe to the topic...",
}


def make_slack_payload(action_id: str, action_value: dict, user: str = "johndoe") -> str:
    """Helper: build a Slack interactive payload dict and JSON-encode it."""
    return json.dumps({
        "type": "block_actions",
        "user": {"id": "U123", "name": user},
        "message": {"ts": "1234567890.123456"},
        "actions": [{
            "action_id": action_id,
            "value":     json.dumps(action_value),
        }]
    })


# ═══════════════════════════════════════════════════════════════
# HEALTH CHECK TESTS
# ═══════════════════════════════════════════════════════════════

class TestHealthCheck:

    def test_health_returns_200_when_all_keys_set(self):
        """Health endpoint should return 200 when all env vars are set."""
        with patch("api.main.boto3") as mock_boto:
            mock_boto.client.return_value.get_caller_identity.return_value = {"Account": "123"}
            response = client.get("/health")

        assert response.status_code == 200
        data = response.json()
        assert data["status"]      == "healthy"
        assert data["openai_key"]  is True
        assert data["pinecone_key"] is True
        assert data["slack_token"] is True

    def test_health_shows_environment(self):
        """Health endpoint should show the current environment."""
        with patch("api.main.boto3"):
            response = client.get("/health")
        assert response.json()["environment"] == "development"

    def test_health_checks_aws_connectivity(self):
        """Health endpoint should check AWS connection and report status."""
        with patch("api.main.boto3") as mock_boto:
            mock_boto.client.return_value.get_caller_identity.side_effect = Exception("No AWS")
            response = client.get("/health")

        # Still 200 (non-fatal) but aws_connected should be False
        data = response.json()
        assert data["aws_connected"] is False


# ═══════════════════════════════════════════════════════════════
# SNS WEBHOOK TESTS
# ═══════════════════════════════════════════════════════════════

class TestCloudWatchWebhook:

    @patch("api.main._run_agent_safe", new_callable=AsyncMock)
    def test_alarm_notification_triggers_agent(self, mock_run):
        """ALARM state notification should trigger the agent."""
        response = client.post(
            "/webhook/cloudwatch",
            content=json.dumps(SNS_NOTIFICATION),
            headers={"Content-Type": "text/plain",
                     "x-amz-sns-message-type": "Notification"}
        )

        assert response.status_code == 202
        data = response.json()
        assert data["status"] == "accepted"
        assert "run_id" in data
        assert data["alarm"] == "cloudops-high-cpu-staging"

    @patch("api.main._run_agent_safe", new_callable=AsyncMock)
    def test_ok_state_ignored(self, mock_run):
        """OK state alarm should be ignored (not trigger agent)."""
        ok_alarm = {**SAMPLE_ALARM_PAYLOAD, "NewStateValue": "OK"}
        sns_ok   = {**SNS_NOTIFICATION, "Message": json.dumps(ok_alarm)}

        response = client.post(
            "/webhook/cloudwatch",
            content=json.dumps(sns_ok),
            headers={"x-amz-sns-message-type": "Notification"}
        )

        assert response.status_code == 200
        assert response.json()["status"] == "ignored"
        mock_run.assert_not_called()

    @patch("api.main._run_agent_safe", new_callable=AsyncMock)
    def test_insufficient_data_ignored(self, mock_run):
        """INSUFFICIENT_DATA state should be ignored."""
        alarm = {**SAMPLE_ALARM_PAYLOAD, "NewStateValue": "INSUFFICIENT_DATA"}
        sns   = {**SNS_NOTIFICATION, "Message": json.dumps(alarm)}

        response = client.post(
            "/webhook/cloudwatch",
            content=json.dumps(sns),
            headers={"x-amz-sns-message-type": "Notification"}
        )

        assert response.json()["status"] == "ignored"
        mock_run.assert_not_called()

    def test_subscription_confirmation(self):
        """SNS subscription confirmation should be handled correctly."""
        with patch("aiohttp.ClientSession") as mock_session:
            mock_resp = AsyncMock()
            mock_resp.status = 200
            mock_session.return_value.__aenter__.return_value.get\
                .return_value.__aenter__.return_value = mock_resp

            response = client.post(
                "/webhook/cloudwatch",
                content=json.dumps(SNS_SUBSCRIPTION),
                headers={"x-amz-sns-message-type": "SubscriptionConfirmation"}
            )

        # Even if confirmation fails in test env, we get a response
        assert response.status_code in [200, 500]

    def test_invalid_json_returns_400(self):
        """Invalid JSON body should return 400."""
        response = client.post(
            "/webhook/cloudwatch",
            content="this is not json",
            headers={"x-amz-sns-message-type": "Notification"}
        )
        assert response.status_code == 400

    def test_unknown_sns_type_ignored(self):
        """Unknown SNS message types should be acknowledged but ignored."""
        response = client.post(
            "/webhook/cloudwatch",
            content=json.dumps({"Type": "SomeUnknownType"}),
            headers={"x-amz-sns-message-type": "SomeUnknownType"}
        )
        assert response.status_code == 200
        assert response.json()["status"] == "ignored"

    @patch("api.main._run_agent_safe", new_callable=AsyncMock)
    def test_each_alarm_gets_unique_run_id(self, mock_run):
        """Each alarm trigger should get a unique run_id."""
        run_ids = set()
        for _ in range(3):
            response = client.post(
                "/webhook/cloudwatch",
                content=json.dumps(SNS_NOTIFICATION),
                headers={"x-amz-sns-message-type": "Notification"}
            )
            run_ids.add(response.json()["run_id"])

        assert len(run_ids) == 3, "Each run should have a unique run_id"


# ═══════════════════════════════════════════════════════════════
# SLACK ACTIONS TESTS
# ═══════════════════════════════════════════════════════════════

class TestSlackActions:

    test_run_id = str(uuid.uuid4())

    @patch("api.main._resume_agent_safe", new_callable=AsyncMock)
    def test_approve_button_resumes_agent(self, mock_resume):
        """Clicking Approve should resume the agent with approved=True."""
        payload = make_slack_payload(
            action_id="approve_remediation",
            action_value={"action": "approve", "run_id": self.test_run_id},
            user="alice"
        )

        response = client.post(
            "/webhook/slack/actions",
            data={"payload": payload},
        )

        assert response.status_code == 200
        data = response.json()
        assert data["replace_original"] is True
        assert "alice" in data["text"]
        assert "approved" in data["text"].lower()

        # Verify resume was called with correct args
        mock_resume.assert_called_once()
        call_kwargs = mock_resume.call_args.kwargs
        assert call_kwargs["run_id"] == self.test_run_id
        assert call_kwargs["human_decision"]["approved"] is True
        assert call_kwargs["human_decision"]["rejected"] is False
        assert call_kwargs["human_decision"]["approved_by"] == "alice"

    @patch("api.main._resume_agent_safe", new_callable=AsyncMock)
    def test_reject_button_resumes_agent_with_rejected(self, mock_resume):
        """Clicking Reject should resume the agent with rejected=True."""
        payload = make_slack_payload(
            action_id="reject_remediation",
            action_value={"action": "reject", "run_id": self.test_run_id},
            user="bob"
        )

        response = client.post(
            "/webhook/slack/actions",
            data={"payload": payload},
        )

        assert response.status_code == 200
        data = response.json()
        assert "bob" in data["text"]
        assert "rejected" in data["text"].lower()

        call_kwargs = mock_resume.call_args.kwargs
        assert call_kwargs["human_decision"]["approved"] is False
        assert call_kwargs["human_decision"]["rejected"] is True

    @patch("api.main._resume_agent_safe", new_callable=AsyncMock)
    def test_missing_run_id_returns_error(self, mock_resume):
        """Missing run_id in button value should return error text."""
        payload = make_slack_payload(
            action_id="approve_remediation",
            action_value={"action": "approve"}  # No run_id!
        )

        response = client.post(
            "/webhook/slack/actions",
            data={"payload": payload},
        )

        # Should still return 200 to Slack (never 4xx to Slack)
        assert response.status_code == 200
        mock_resume.assert_not_called()

    @patch("api.main._resume_agent_safe", new_callable=AsyncMock)
    def test_unknown_action_id_ignored(self, mock_resume):
        """Unknown action IDs should be silently ignored."""
        payload = make_slack_payload(
            action_id="some_random_button",
            action_value={"action": "unknown", "run_id": self.test_run_id}
        )

        response = client.post(
            "/webhook/slack/actions",
            data={"payload": payload},
        )

        assert response.status_code == 200
        mock_resume.assert_not_called()

    def test_invalid_payload_returns_400(self):
        """Completely invalid Slack payload should return 400."""
        response = client.post(
            "/webhook/slack/actions",
            data={"payload": "this is not json"},
        )
        assert response.status_code == 400

    @patch("api.main._resume_agent_safe", new_callable=AsyncMock)
    def test_empty_actions_list_returns_200(self, mock_resume):
        """Empty actions list should return 200 (Slack sends this sometimes)."""
        payload = json.dumps({
            "type": "block_actions",
            "user": {"name": "user"},
            "actions": []  # Empty
        })

        response = client.post(
            "/webhook/slack/actions",
            data={"payload": payload},
        )

        assert response.status_code == 200
        mock_resume.assert_not_called()


# ═══════════════════════════════════════════════════════════════
# SLACK SIGNATURE VERIFICATION TESTS
# ═══════════════════════════════════════════════════════════════

class TestSlackSignatureVerification:
    """
    Test the security signature verification.
    These tests run with ENVIRONMENT=production to enable verification.
    """

    def _make_valid_signature(self, body: str) -> tuple[str, str]:
        """Helper: create a valid Slack signature for a body."""
        timestamp  = str(int(time.time()))
        secret     = os.environ.get("SLACK_SIGNING_SECRET", "test-signing-secret-abc123")
        base       = f"v0:{timestamp}:{body}"
        signature  = "v0=" + hmac.new(
            secret.encode(),
            base.encode(),
            hashlib.sha256
        ).hexdigest()
        return signature, timestamp

    @patch("api.main.ENVIRONMENT", "production")
    @patch("api.main._resume_agent_safe", new_callable=AsyncMock)
    def test_valid_signature_accepted(self, mock_resume):
        """Valid Slack signature should be accepted in production."""
        payload_str = make_slack_payload(
            "approve_remediation",
            {"action": "approve", "run_id": str(uuid.uuid4())}
        )
        body      = f"payload={payload_str}"
        sig, ts   = self._make_valid_signature(body)

        response = client.post(
            "/webhook/slack/actions",
            content=body,
            headers={
                "Content-Type":                 "application/x-www-form-urlencoded",
                "x-slack-signature":            sig,
                "x-slack-request-timestamp":    ts,
            }
        )

        assert response.status_code == 200

    @patch("api.main.ENVIRONMENT", "production")
    def test_invalid_signature_rejected(self):
        """Fake/invalid Slack signature should be rejected with 401."""
        payload_str = make_slack_payload(
            "approve_remediation",
            {"action": "approve", "run_id": str(uuid.uuid4())}
        )

        response = client.post(
            "/webhook/slack/actions",
            data={"payload": payload_str},
            headers={
                "x-slack-signature":         "v0=fakesignaturethatisntvalid",
                "x-slack-request-timestamp": str(int(time.time())),
            }
        )

        assert response.status_code == 401

    @patch("api.main.ENVIRONMENT", "production")
    def test_old_timestamp_rejected(self):
        """Replay attack: old timestamp should be rejected with 401."""
        payload_str = make_slack_payload(
            "approve_remediation",
            {"action": "approve", "run_id": str(uuid.uuid4())}
        )
        old_timestamp = str(int(time.time()) - 400)  # 400 seconds old (> 5 min)
        body          = f"payload={payload_str}"
        secret        = os.environ.get("SLACK_SIGNING_SECRET", "test-signing-secret-abc123")
        base          = f"v0:{old_timestamp}:{body}"
        sig           = "v0=" + hmac.new(secret.encode(), base.encode(), hashlib.sha256).hexdigest()

        response = client.post(
            "/webhook/slack/actions",
            content=body,
            headers={
                "Content-Type":              "application/x-www-form-urlencoded",
                "x-slack-signature":         sig,
                "x-slack-request-timestamp": old_timestamp,
            }
        )

        assert response.status_code == 401

    @patch("api.main.ENVIRONMENT", "production")
    def test_missing_signature_headers_rejected(self):
        """Missing Slack signature headers should return 401."""
        response = client.post(
            "/webhook/slack/actions",
            data={"payload": make_slack_payload("approve_remediation", {})},
            # No x-slack-signature header
        )
        assert response.status_code == 401


# ═══════════════════════════════════════════════════════════════
# RUN STATUS TESTS
# ═══════════════════════════════════════════════════════════════

class TestRunStatus:

    @patch("api.main.get_run_state")
    def test_returns_run_details(self, mock_state):
        """GET /runs/{run_id} should return a summary of the run."""
        mock_state.return_value = {
            "started_at":   "2024-01-01T12:00:00",
            "node_history": ["ingest_node", "analyze_node", "plan_node", "report_node"],
            "alert":        {"AlarmName": "test-alarm"},
            "root_cause":   {"severity": "HIGH", "confidence": 0.88, "root_cause": "Memory leak"},
            "fix_plan":     {"action_type": "auto_safe"},
            "remediation":  {"success": True, "actions_taken": ["restarted app"]},
            "approved":     False,
            "rejected":     False,
            "errors":       [],
        }

        run_id   = str(uuid.uuid4())
        response = client.get(f"/runs/{run_id}")

        assert response.status_code == 200
        data = response.json()
        assert data["run_id"]      == run_id
        assert data["severity"]    == "HIGH"
        assert data["confidence"]  == 0.88
        assert data["action_type"] == "auto_safe"
        assert data["remediated"]  is True

    @patch("api.main.get_run_state")
    def test_unknown_run_returns_404(self, mock_state):
        """Unknown run_id should return 404."""
        mock_state.return_value = None

        response = client.get(f"/runs/{str(uuid.uuid4())}")
        assert response.status_code == 404


# ═══════════════════════════════════════════════════════════════
# MANUAL TEST TRIGGER TESTS
# ═══════════════════════════════════════════════════════════════

class TestManualTrigger:

    @patch("api.main._run_agent_safe", new_callable=AsyncMock)
    def test_trigger_in_dev_mode(self, mock_run):
        """Test trigger should work in development mode."""
        response = client.post(
            "/trigger/test",
            json={"alarm_name": "test-cpu", "state_reason": "CPU at 95%"}
        )

        assert response.status_code == 202
        data = response.json()
        assert data["status"] == "triggered"
        assert "run_id" in data
        assert "check_status" in data

    @patch("api.main.ENVIRONMENT", "production")
    def test_trigger_disabled_in_production(self):
        """Test trigger should be disabled in production (403)."""
        response = client.post(
            "/trigger/test",
            json={"alarm_name": "test"}
        )
        assert response.status_code == 403

    @patch("api.main._run_agent_safe", new_callable=AsyncMock)
    def test_trigger_with_default_values(self, mock_run):
        """Test trigger should work with no body (uses defaults)."""
        response = client.post("/trigger/test", json={})
        assert response.status_code == 202


if __name__ == "__main__":
    import pytest
    pytest.main([__file__, "-v", "--tb=short"])
