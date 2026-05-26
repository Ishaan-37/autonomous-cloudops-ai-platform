"""
tests/test_agent.py
-------------------
COMPLETE TEST SUITE FOR PHASE 4.

Run with:
  pytest tests/test_agent.py -v

Tests cover:
  - Each node in isolation (unit tests)
  - The full graph with a fake alarm (integration test)
  - Edge cases: missing data, LLM failures, SSM errors
  - The approval flow (human approve + reject)

We use unittest.mock to avoid:
  - Real OpenAI API calls (costs money, slow)
  - Real AWS API calls (modifies real infrastructure)
  - Real Pinecone calls (requires connection)
  - Real Slack calls (sends real messages)
"""

import asyncio
import json
import pytest
import uuid
from unittest.mock import AsyncMock, MagicMock, patch

from agent.state import initial_state, AgentState
from agent.nodes.ingest_node    import ingest_node
from agent.nodes.analyze_node   import analyze_node, _fallback_rca
from agent.nodes.plan_node      import plan_node, is_command_allowed
from agent.nodes.remediate_node import remediate_node, validate_ec2_instance
from agent.nodes.report_node    import report_node, determine_outcome


# ── Test Fixtures ─────────────────────────────────────────────

SAMPLE_ALARM = {
    "AlarmName":    "cloudops-high-cpu-staging",
    "AlarmArn":     "arn:aws:cloudwatch:us-east-1:123456789:alarm:cloudops-high-cpu-staging",
    "NewStateValue": "ALARM",
    "OldStateValue": "OK",
    "StateReason":  "Threshold Crossed: 1 datapoint [92.3 (01/01/24 12:00:00)] was greater than the threshold (85.0).",
    "Region":       "us-east-1",
    "Trigger": {
        "MetricName":  "CPUUtilization",
        "Namespace":   "AWS/EC2",
        "Dimensions":  [{"name": "InstanceId", "value": "i-0abc1234def56789"}],
        "Threshold":   85.0,
    }
}

SAMPLE_RCA = {
    "root_cause":           "High CPU caused by a runaway Python process consuming 95% CPU on instance i-0abc1234def56789. Process started after a bad deployment at 11:45 UTC.",
    "confidence":           0.88,
    "severity":             "HIGH",
    "category":             "cpu",
    "affected_resources":   ["i-0abc1234def56789"],
    "symptoms":             ["CPU at 92%", "Process running for 3 hours"],
    "contributing_factors": ["Bad deployment", "No CPU limit set on process"]
}

SAMPLE_FIX_PLAN = {
    "summary":           "Restart the application service via SSM Run Command",
    "steps":             ["1. Send SSM command to restart app service", "2. Monitor CPU for 5 minutes"],
    "action_type":       "auto_safe",
    "estimated_risk":    "low",
    "estimated_time":    "30 seconds",
    "rollback_plan":     "If service doesn't start, SSH in and check systemd logs",
    "requires_approval": False,
}


def make_state(overrides: dict = {}) -> AgentState:
    """Helper: create a test state with optional field overrides."""
    state = initial_state(alert=SAMPLE_ALARM, run_id=str(uuid.uuid4()))
    state.update(overrides)
    return state


# ═══════════════════════════════════════════════════════════════
# UNIT TESTS — each node in isolation
# ═══════════════════════════════════════════════════════════════

class TestIngestNode:

    @pytest.mark.asyncio
    @patch("agent.nodes.ingest_node.query_logs",  new_callable=AsyncMock)
    @patch("agent.nodes.ingest_node.query_docs",  new_callable=AsyncMock)
    @patch("agent.nodes.ingest_node.fetch_live_logs", new_callable=AsyncMock)
    async def test_ingest_node_success(self, mock_live, mock_docs, mock_logs):
        """ingest_node should populate log_context and doc_context."""
        mock_logs.return_value = [
            {"text": "ERROR OOM kill on i-0abc123", "score": 0.92, "metadata": {"severity": "ERROR", "instance_id": "i-0abc123", "timestamp": "2024-01-01T12:00:00"}}
        ]
        mock_docs.return_value = [
            {"text": "High CPU can be caused by...", "score": 0.85, "section": "EC2 Troubleshooting", "source": "https://docs.aws.amazon.com/..."}
        ]
        mock_live.return_value = []

        state  = make_state()
        result = await ingest_node(state)

        assert "log_context"  in result
        assert "doc_context"  in result
        assert "node_history" in result
        assert "ingest_node"  in result["node_history"]
        assert len(result["log_context"]) > 0

    @pytest.mark.asyncio
    @patch("agent.nodes.ingest_node.query_logs",  new_callable=AsyncMock)
    @patch("agent.nodes.ingest_node.query_docs",  new_callable=AsyncMock)
    @patch("agent.nodes.ingest_node.fetch_live_logs", new_callable=AsyncMock)
    async def test_ingest_node_no_logs(self, mock_live, mock_docs, mock_logs):
        """ingest_node should handle empty log results gracefully."""
        mock_logs.return_value = []
        mock_docs.return_value = []
        mock_live.return_value = []

        state  = make_state()
        result = await ingest_node(state)

        # Should still return the fields, just with empty context
        assert "log_context"  in result
        assert "node_history" in result
        # Should add an error note
        assert any("no logs" in e.lower() for e in result.get("errors", []))


class TestAnalyzeNode:

    @pytest.mark.asyncio
    @patch("agent.nodes.analyze_node.oai")
    async def test_analyze_node_success(self, mock_oai):
        """analyze_node should parse GPT-4 JSON response into RootCauseAnalysis."""
        mock_response = MagicMock()
        mock_response.choices[0].message.content = json.dumps(SAMPLE_RCA)
        mock_oai.chat.completions.create = AsyncMock(return_value=mock_response)

        state = make_state({"log_context": "ERROR: high CPU", "doc_context": "EC2 docs"})
        result = await analyze_node(state)

        assert result["root_cause"] is not None
        assert result["root_cause"]["severity"]   == "HIGH"
        assert result["root_cause"]["confidence"] == 0.88
        assert "i-0abc1234def56789" in result["root_cause"]["affected_resources"]
        assert "analyze_node" in result["node_history"]

    @pytest.mark.asyncio
    @patch("agent.nodes.analyze_node.oai")
    async def test_analyze_node_bad_json(self, mock_oai):
        """analyze_node should use fallback RCA when GPT-4 returns invalid JSON."""
        mock_response = MagicMock()
        mock_response.choices[0].message.content = "This is not JSON at all"
        mock_oai.chat.completions.create = AsyncMock(return_value=mock_response)

        state  = make_state()
        result = await analyze_node(state)

        # Should return fallback, not crash
        assert result["root_cause"] is not None
        assert result["root_cause"]["confidence"] == 0.0
        assert result["root_cause"]["severity"]   == "HIGH"  # Fallback assumes HIGH
        assert len(result["errors"]) > 0

    @pytest.mark.asyncio
    @patch("agent.nodes.analyze_node.oai")
    async def test_analyze_node_api_error(self, mock_oai):
        """analyze_node should handle OpenAI API failures gracefully."""
        mock_oai.chat.completions.create = AsyncMock(side_effect=Exception("API timeout"))

        state  = make_state()
        result = await analyze_node(state)

        assert result["root_cause"] is not None  # Fallback returned
        assert "analyze_node" in result["errors"][0]

    def test_fallback_rca(self):
        """_fallback_rca should always return a valid RCA structure."""
        rca = _fallback_rca(SAMPLE_ALARM, "Test error")
        assert rca["severity"]   == "HIGH"
        assert rca["confidence"] == 0.0
        assert "manual review" in rca["root_cause"].lower()


class TestPlanNode:

    @pytest.mark.asyncio
    @patch("agent.nodes.plan_node.oai")
    async def test_plan_node_auto_safe(self, mock_oai):
        """plan_node should return auto_safe for low-risk fixes."""
        plan_json = {
            **SAMPLE_FIX_PLAN,
            "ssm_commands": ["sudo systemctl restart app"],
            "notes": ""
        }
        mock_response = MagicMock()
        mock_response.choices[0].message.content = json.dumps(plan_json)
        mock_oai.chat.completions.create = AsyncMock(return_value=mock_response)

        state  = make_state({"root_cause": SAMPLE_RCA})
        result = await plan_node(state)

        assert result["fix_plan"]["action_type"] == "auto_safe"
        assert result["fix_plan"]["requires_approval"] is False
        assert "plan_node" in result["node_history"]

    @pytest.mark.asyncio
    @patch("agent.nodes.plan_node.oai")
    async def test_plan_node_critical_forces_approval(self, mock_oai):
        """plan_node should force requires_approval=True when severity is CRITICAL."""
        plan_json = {
            **SAMPLE_FIX_PLAN,
            "action_type": "auto_safe",  # GPT-4 says safe...
            "ssm_commands": ["sudo systemctl restart app"],
            "notes": ""
        }
        mock_response = MagicMock()
        mock_response.choices[0].message.content = json.dumps(plan_json)
        mock_oai.chat.completions.create = AsyncMock(return_value=mock_response)

        critical_rca = {**SAMPLE_RCA, "severity": "CRITICAL"}
        state  = make_state({"root_cause": critical_rca})
        result = await plan_node(state)

        # Should escalate because severity is CRITICAL
        assert result["fix_plan"]["requires_approval"] is True

    def test_command_safety_whitelist(self):
        """is_command_allowed should block dangerous commands."""
        assert is_command_allowed("sudo journalctl --vacuum-size=500M") is True
        assert is_command_allowed("sudo systemctl restart nginx")        is True
        assert is_command_allowed("df -h")                               is True
        assert is_command_allowed("sudo rm -rf /")                       is False
        assert is_command_allowed("reboot")                              is False
        assert is_command_allowed("dd if=/dev/zero of=/dev/sda")         is False
        assert is_command_allowed("DROP TABLE users")                    is False


class TestRemediateNode:

    @pytest.mark.asyncio
    async def test_remediate_skips_manual_only(self):
        """remediate_node should skip execution for manual_only plans."""
        manual_plan = {**SAMPLE_FIX_PLAN, "action_type": "manual_only"}
        state  = make_state({"root_cause": SAMPLE_RCA, "fix_plan": manual_plan})
        result = await remediate_node(state)

        assert result["remediation"]["success"]        is True
        assert "manual_only" in result["remediation"]["actions_taken"][0].lower()

    @pytest.mark.asyncio
    async def test_remediate_blocks_unapproved_risky(self):
        """remediate_node should block auto_risky actions without approval."""
        risky_plan = {**SAMPLE_FIX_PLAN, "action_type": "auto_risky"}
        state = make_state({
            "root_cause": SAMPLE_RCA,
            "fix_plan":   risky_plan,
            "approved":   False,   # Not approved
        })
        result = await remediate_node(state)

        assert any("unapproved" in e.lower() for e in result.get("errors", []))

    @pytest.mark.asyncio
    async def test_remediate_skips_when_rejected(self):
        """remediate_node should not run if human rejected."""
        state = make_state({
            "root_cause": SAMPLE_RCA,
            "fix_plan":   SAMPLE_FIX_PLAN,
            "rejected":   True,
        })
        result = await remediate_node(state)
        assert "rejected" in result["remediation"]["actions_taken"][0].lower()

    def test_validate_ec2_instance_id_format(self):
        """validate_ec2_instance should reject malformed instance IDs."""
        # We can't call real AWS in tests, so just test the format check
        # A real test would mock boto3
        import asyncio
        async def run():
            ok, err = await validate_ec2_instance("not-an-instance-id")
            assert ok is False
            assert "Invalid" in err
        asyncio.run(run())


class TestReportNode:

    def test_determine_outcome_success(self):
        state = make_state({
            "fix_plan":    {**SAMPLE_FIX_PLAN, "action_type": "auto_safe"},
            "remediation": {"success": True, "actions_taken": ["did something"], "action_ids": [], "error": None, "timestamp": "2024"},
            "rejected":    False,
            "errors":      [],
        })
        assert determine_outcome(state) == "SUCCESS"

    def test_determine_outcome_rejected(self):
        state = make_state({"rejected": True})
        assert determine_outcome(state) == "REJECTED"

    def test_determine_outcome_manual(self):
        state = make_state({
            "fix_plan": {**SAMPLE_FIX_PLAN, "action_type": "manual_only"},
            "rejected": False,
        })
        assert determine_outcome(state) == "MANUAL"

    def test_determine_outcome_info(self):
        state = make_state({
            "fix_plan": {**SAMPLE_FIX_PLAN, "action_type": "no_action"},
            "rejected": False,
        })
        assert determine_outcome(state) == "INFO"

    @pytest.mark.asyncio
    @patch("agent.nodes.report_node.slack")
    async def test_report_node_always_runs(self, mock_slack):
        """report_node should complete even with an empty state (failed earlier nodes)."""
        mock_slack.chat_postMessage = AsyncMock(return_value={"ts": "123.456"})

        # Completely empty state — all previous nodes "failed"
        state  = make_state()
        result = await report_node(state)

        assert "report"       in result
        assert "node_history" in result
        assert "report_node"  in result["node_history"]
        mock_slack.chat_postMessage.assert_called_once()


# ═══════════════════════════════════════════════════════════════
# INTEGRATION TEST — full graph with fake alarm
# ═══════════════════════════════════════════════════════════════

class TestFullGraph:

    @pytest.mark.asyncio
    @patch("agent.nodes.ingest_node.query_logs",      new_callable=AsyncMock)
    @patch("agent.nodes.ingest_node.query_docs",      new_callable=AsyncMock)
    @patch("agent.nodes.ingest_node.fetch_live_logs", new_callable=AsyncMock)
    @patch("agent.nodes.analyze_node.oai")
    @patch("agent.nodes.plan_node.oai")
    @patch("agent.nodes.remediate_node.ec2")
    @patch("agent.nodes.remediate_node.ssm")
    @patch("agent.nodes.remediate_node.dynamodb")
    @patch("agent.nodes.report_node.slack")
    async def test_full_auto_safe_run(
        self, mock_slack, mock_dynamo, mock_ssm, mock_ec2,
        mock_plan_oai, mock_analyze_oai, mock_live, mock_docs, mock_logs
    ):
        """
        Full end-to-end test: alarm → analyze → plan → auto_safe → remediate → report.
        No human approval needed (auto_safe path).
        """
        from langgraph.checkpoint.memory import MemorySaver
        from agent.graph import build_agent_graph, run_agent

        # ── Mock all external dependencies ────────────────────
        mock_logs.return_value = []
        mock_docs.return_value = []
        mock_live.return_value = []

        # GPT-4 for analyze
        analyze_response = MagicMock()
        analyze_response.choices[0].message.content = json.dumps(SAMPLE_RCA)
        mock_analyze_oai.chat.completions.create = AsyncMock(return_value=analyze_response)

        # GPT-4 for plan
        plan_response = MagicMock()
        plan_response.choices[0].message.content = json.dumps({
            **SAMPLE_FIX_PLAN,
            "ssm_commands": ["sudo journalctl --vacuum-size=500M"],
            "notes": ""
        })
        mock_plan_oai.chat.completions.create = AsyncMock(return_value=plan_response)

        # EC2 validate
        mock_ec2.describe_instances.return_value = {
            "Reservations": [{"Instances": [{"State": {"Name": "running"}}]}]
        }

        # SSM run + poll
        mock_ssm.send_command.return_value = {"Command": {"CommandId": "cmd-test-123"}}
        mock_ssm.get_command_invocation.return_value = {
            "Status": "Success",
            "StandardOutputContent": "Deleted 500MB of journal logs",
            "StandardErrorContent": ""
        }

        # DynamoDB
        mock_table = MagicMock()
        mock_table.put_item = MagicMock()
        mock_dynamo.Table.return_value = mock_table

        # Slack
        mock_slack.chat_postMessage = AsyncMock(return_value={"ts": "test.ts.123"})

        # ── Build graph with in-memory checkpointer ────────────
        memory_checkpointer = MemorySaver()
        test_agent = build_agent_graph(checkpointer=memory_checkpointer)

        # ── Run the agent ──────────────────────────────────────
        run_id = str(uuid.uuid4())
        state  = initial_state(alert=SAMPLE_ALARM, run_id=run_id)
        config = {"configurable": {"thread_id": run_id}}

        result = await test_agent.ainvoke(state, config=config)

        # ── Assertions ────────────────────────────────────────
        assert result is not None
        assert result.get("root_cause") is not None
        assert result["root_cause"]["severity"] == "HIGH"
        assert result.get("fix_plan") is not None
        assert result["fix_plan"]["action_type"] == "auto_safe"
        assert result.get("remediation") is not None
        assert result["remediation"]["success"] is True
        assert result.get("report") is not None
        assert "report_node" in result["node_history"]

        # Verify all 6 nodes ran
        expected_nodes = ["ingest_node", "analyze_node", "plan_node",
                          "remediate_node", "report_node"]
        for node in expected_nodes:
            assert node in result["node_history"], f"Missing node: {node}"


if __name__ == "__main__":
    pytest.main([__file__, "-v", "--tb=short"])
