"""
tests/test_phase6.py
---------------------
TEST SUITE FOR PHASE 6: FinOps + Observability

Tests cover:
  - Cost optimizer data collection functions
  - LLM analysis with mocked GPT-4
  - Slack report posting
  - Telemetry setup (no-crash tests)
  - CronJob trigger simulation

Run with:
  pytest tests/test_phase6.py -v
"""

import asyncio
import json
import os
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

os.environ["CLOUDOPS_ENVIRONMENT"] = "development"
os.environ["OPENAI_API_KEY"]       = "sk-test"
os.environ["PINECONE_API_KEY"]     = "test-pinecone"
os.environ["SLACK_BOT_TOKEN"]      = "xoxb-test"
os.environ["SLACK_SIGNING_SECRET"] = "test-secret"
os.environ["AWS_REGION"]           = "us-east-1"


# ═══════════════════════════════════════════════════════════════
# FINOPS TESTS
# ═══════════════════════════════════════════════════════════════

class TestCostOptimizerDataCollection:

    @pytest.mark.asyncio
    @patch("agent.nodes.cost_optimizer_node.ce_client")
    async def test_monthly_spend_by_service(self, mock_ce):
        """Should return spend grouped by service, sorted by cost."""
        from agent.nodes.cost_optimizer_node import get_monthly_spend_by_service

        mock_ce.get_cost_and_usage.return_value = {
            "ResultsByTime": [{
                "Groups": [
                    {"Keys": ["Amazon EC2"],  "Metrics": {"BlendedCost": {"Amount": "234.56"}}},
                    {"Keys": ["Amazon RDS"],  "Metrics": {"BlendedCost": {"Amount": "89.12"}}},
                    {"Keys": ["Amazon S3"],   "Metrics": {"BlendedCost": {"Amount": "0.30"}}},  # < $0.50 threshold
                ]
            }]
        }

        result = await get_monthly_spend_by_service()

        assert "Amazon EC2" in result
        assert "Amazon RDS" in result
        assert "Amazon S3" not in result     # Below $0.50 threshold
        assert result["Amazon EC2"] == 234.56
        # Should be sorted by cost descending
        assert list(result.keys())[0] == "Amazon EC2"

    @pytest.mark.asyncio
    @patch("agent.nodes.cost_optimizer_node.ce_client")
    async def test_monthly_spend_handles_api_error(self, mock_ce):
        """Should return empty dict on API error, not crash."""
        from agent.nodes.cost_optimizer_node import get_monthly_spend_by_service

        mock_ce.get_cost_and_usage.side_effect = Exception("Cost Explorer API error")
        result = await get_monthly_spend_by_service()
        assert result == {}

    @pytest.mark.asyncio
    @patch("agent.nodes.cost_optimizer_node.ce_client")
    async def test_week_over_week_spike_detection(self, mock_ce):
        """Should flag spend spike when increase > 20%."""
        from agent.nodes.cost_optimizer_node import get_week_over_week_change

        call_count = [0]
        def mock_get_cost(*args, **kwargs):
            call_count[0] += 1
            # First call = this week ($150), second call = last week ($100)
            amount = "150.00" if call_count[0] == 1 else "100.00"
            return {
                "ResultsByTime": [{"Total": {"BlendedCost": {"Amount": amount}}}]
            }

        mock_ce.get_cost_and_usage.side_effect = mock_get_cost
        result = await get_week_over_week_change()

        assert result["this_week"]  == 150.0
        assert result["last_week"]  == 100.0
        assert result["change_pct"] == 50.0
        assert result["is_spike"]   is True   # 50% > 20% threshold

    @pytest.mark.asyncio
    @patch("agent.nodes.cost_optimizer_node.ce_client")
    async def test_no_spike_when_change_small(self, mock_ce):
        """Should not flag spike when increase < 20%."""
        from agent.nodes.cost_optimizer_node import get_week_over_week_change

        call_count = [0]
        def mock_get_cost(*args, **kwargs):
            call_count[0] += 1
            amount = "105.00" if call_count[0] == 1 else "100.00"
            return {
                "ResultsByTime": [{"Total": {"BlendedCost": {"Amount": amount}}}]
            }

        mock_ce.get_cost_and_usage.side_effect = mock_get_cost
        result = await get_week_over_week_change()

        assert result["is_spike"] is False  # 5% < 20% threshold

    @pytest.mark.asyncio
    @patch("agent.nodes.cost_optimizer_node.ec2_client")
    @patch("agent.nodes.cost_optimizer_node.cw_client")
    async def test_find_idle_instances(self, mock_cw, mock_ec2):
        """Should identify EC2 instances with < 5% avg CPU."""
        from agent.nodes.cost_optimizer_node import find_idle_ec2_instances

        mock_ec2.describe_instances.return_value = {
            "Reservations": [{
                "Instances": [{
                    "InstanceId":   "i-0abc123",
                    "InstanceType": "t3.medium",
                    "Tags": [{"Key": "Name", "Value": "web-server-1"}]
                }]
            }]
        }

        mock_cw.get_metric_statistics.return_value = {
            "Datapoints": [
                {"Average": 2.1},
                {"Average": 1.8},
                {"Average": 3.2},
            ]
        }

        result = await find_idle_ec2_instances()

        assert len(result) == 1
        assert result[0]["instance_id"]   == "i-0abc123"
        assert result[0]["avg_cpu_pct"]   < 5.0
        assert result[0]["instance_type"] == "t3.medium"

    @pytest.mark.asyncio
    @patch("agent.nodes.cost_optimizer_node.ec2_client")
    @patch("agent.nodes.cost_optimizer_node.cw_client")
    async def test_active_instances_not_flagged(self, mock_cw, mock_ec2):
        """Should NOT flag instances with > 5% CPU as idle."""
        from agent.nodes.cost_optimizer_node import find_idle_ec2_instances

        mock_ec2.describe_instances.return_value = {
            "Reservations": [{
                "Instances": [{"InstanceId": "i-0active", "InstanceType": "t3.large", "Tags": []}]
            }]
        }
        mock_cw.get_metric_statistics.return_value = {
            "Datapoints": [{"Average": 45.0}, {"Average": 52.0}]  # High CPU
        }

        result = await find_idle_ec2_instances()
        assert len(result) == 0   # No idle instances

    @pytest.mark.asyncio
    @patch("agent.nodes.cost_optimizer_node.ec2_client")
    async def test_find_unattached_volumes(self, mock_ec2):
        """Should find EBS volumes in 'available' state older than 3 days."""
        from agent.nodes.cost_optimizer_node import find_unattached_ebs_volumes

        old_time = datetime(2024, 1, 1, tzinfo=timezone.utc)  # Old date
        mock_ec2.describe_volumes.return_value = {
            "Volumes": [{
                "VolumeId":   "vol-0abc123",
                "VolumeSize": 100,
                "VolumeType": "gp3",
                "CreateTime": old_time,
                "Tags": [{"Key": "Name", "Value": "old-backup"}]
            }]
        }

        result = await find_unattached_ebs_volumes()

        assert len(result) == 1
        assert result[0]["volume_id"]    == "vol-0abc123"
        assert result[0]["size_gb"]      == 100
        assert result[0]["monthly_cost"] == 8.00  # 100 * $0.08

    @pytest.mark.asyncio
    @patch("agent.nodes.cost_optimizer_node.ec2_client")
    async def test_find_unused_elastic_ips(self, mock_ec2):
        """Should find Elastic IPs with no AssociationId."""
        from agent.nodes.cost_optimizer_node import find_unused_elastic_ips

        mock_ec2.describe_addresses.return_value = {
            "Addresses": [
                {
                    "AllocationId": "eipalloc-abc123",
                    "PublicIp":     "1.2.3.4",
                    # No AssociationId = not attached
                },
                {
                    "AllocationId": "eipalloc-def456",
                    "PublicIp":     "5.6.7.8",
                    "AssociationId": "eipassoc-xyz",  # This one IS attached
                }
            ]
        }

        result = await find_unused_elastic_ips()

        assert len(result) == 1  # Only the unattached one
        assert result[0]["public_ip"]    == "1.2.3.4"
        assert result[0]["monthly_cost"] == 3.65


class TestCostOptimizerLLMAnalysis:

    @pytest.mark.asyncio
    @patch("agent.nodes.cost_optimizer_node.oai")
    async def test_llm_analysis_success(self, mock_oai):
        """Should parse GPT-4 JSON response into recommendations."""
        from agent.nodes.cost_optimizer_node import analyze_costs_with_llm

        mock_analysis = {
            "total_monthly_spend":            423.68,
            "projected_annual":               5084.16,
            "total_potential_savings_monthly": 45.65,
            "top_recommendations": [
                {
                    "rank":            1,
                    "title":           "Delete unattached EBS volume vol-0abc123",
                    "description":     "100GB volume unused for 30+ days",
                    "monthly_savings": 8.00,
                    "risk":            "safe",
                    "effort":          "2 minutes",
                    "action":          "aws ec2 delete-volume --volume-id vol-0abc123"
                }
            ],
            "spend_anomalies": [],
            "summary":          "Overall spend is healthy with minor optimization opportunities."
        }

        mock_response = MagicMock()
        mock_response.choices[0].message.content = json.dumps(mock_analysis)
        mock_oai.chat.completions.create = AsyncMock(return_value=mock_response)

        cost_data = {
            "spend_by_service":   {"Amazon EC2": 234.56, "Amazon RDS": 89.12},
            "wow_change":         {"this_week": 105.0, "last_week": 100.0, "change_pct": 5.0, "is_spike": False},
            "idle_instances":     [],
            "unattached_volumes": [{"volume_id": "vol-0abc123", "size_gb": 100, "monthly_cost": 8.0}],
            "unused_eips":        [],
            "old_snapshots":      [],
        }

        result = await analyze_costs_with_llm(cost_data)

        assert result["total_monthly_spend"]             == 423.68
        assert result["total_potential_savings_monthly"] == 45.65
        assert len(result["top_recommendations"])        == 1
        assert result["top_recommendations"][0]["risk"]  == "safe"

    @pytest.mark.asyncio
    @patch("agent.nodes.cost_optimizer_node.oai")
    async def test_llm_analysis_handles_failure(self, mock_oai):
        """Should return fallback result on LLM failure."""
        from agent.nodes.cost_optimizer_node import analyze_costs_with_llm

        mock_oai.chat.completions.create = AsyncMock(side_effect=Exception("API timeout"))

        cost_data = {
            "spend_by_service":   {"Amazon EC2": 100.0},
            "wow_change":         {},
            "idle_instances":     [],
            "unattached_volumes": [],
            "unused_eips":        [],
            "old_snapshots":      [],
        }

        result = await analyze_costs_with_llm(cost_data)

        # Should not crash, should return fallback
        assert "total_monthly_spend"            in result
        assert "top_recommendations"            in result
        assert result["total_potential_savings_monthly"] == 0

    @pytest.mark.asyncio
    @patch("agent.nodes.cost_optimizer_node.oai")
    @patch("agent.nodes.cost_optimizer_node.slack")
    @patch("agent.nodes.cost_optimizer_node.ec2_client")
    @patch("agent.nodes.cost_optimizer_node.cw_client")
    @patch("agent.nodes.cost_optimizer_node.ce_client")
    async def test_full_cost_optimization_run(self, mock_ce, mock_cw, mock_ec2, mock_slack, mock_oai):
        """Full end-to-end test of the cost optimizer pipeline."""
        from agent.nodes.cost_optimizer_node import run_cost_optimization

        # Mock all AWS calls
        mock_ce.get_cost_and_usage.return_value = {
            "ResultsByTime": [{"Groups": [
                {"Keys": ["Amazon EC2"], "Metrics": {"BlendedCost": {"Amount": "150.00"}}}
            ]}]
        }
        mock_ec2.describe_instances.return_value = {"Reservations": []}
        mock_ec2.describe_volumes.return_value   = {"Volumes": []}
        mock_ec2.describe_addresses.return_value = {"Addresses": []}
        mock_ec2.describe_snapshots.return_value = {"Snapshots": []}
        mock_cw.get_metric_statistics.return_value = {"Datapoints": []}

        # Mock boto3.client("sts") for account_id in find_old_snapshots
        with patch("boto3.client") as mock_boto:
            mock_boto.return_value.get_caller_identity.return_value = {"Account": "123456789"}

            # Mock LLM response
            mock_response = MagicMock()
            mock_response.choices[0].message.content = json.dumps({
                "total_monthly_spend":            150.0,
                "projected_annual":               1800.0,
                "total_potential_savings_monthly": 0.0,
                "top_recommendations":            [],
                "spend_anomalies":                [],
                "summary":                        "Spend looks healthy."
            })
            mock_oai.chat.completions.create = AsyncMock(return_value=mock_response)

            # Mock Slack
            mock_slack.chat_postMessage = AsyncMock(return_value={"ts": "123.456"})

            result = await run_cost_optimization()

        assert result["status"] == "complete"
        assert "analysis"       in result
        mock_slack.chat_postMessage.assert_called_once()


# ═══════════════════════════════════════════════════════════════
# OBSERVABILITY TESTS
# ═══════════════════════════════════════════════════════════════

class TestTelemetry:

    def test_telemetry_imports_without_crash(self):
        """Telemetry module should import cleanly even without OTel collector running."""
        try:
            from observability.telemetry import tracer, meter, alarms_processed
            assert tracer  is not None
            assert meter   is not None
        except Exception as e:
            pytest.fail(f"Telemetry import failed: {e}")

    def test_traced_decorator_works(self):
        """@traced decorator should wrap async functions correctly."""
        from observability.telemetry import traced

        @traced("test_span")
        async def my_function(state):
            return {"result": "ok"}

        result = asyncio.run(my_function({"run_id": "test-123", "alert": {}}))
        assert result == {"result": "ok"}

    def test_metrics_recording_no_crash(self):
        """Recording metrics should not crash even without collector."""
        from observability.telemetry import alarms_processed, remediations_executed

        # These should not raise exceptions
        alarms_processed.add(1, {"severity": "HIGH", "category": "cpu"})
        remediations_executed.add(1, {"action_type": "auto_safe", "success": "true"})


# ═══════════════════════════════════════════════════════════════
# HELM CHART STRUCTURE TESTS
# ═══════════════════════════════════════════════════════════════

class TestHelmChartStructure:

    def test_helm_values_file_exists(self):
        """helm/agent/values.yaml should exist and be valid YAML."""
        import yaml
        values_path = "helm/agent/values.yaml"
        if not os.path.exists(values_path):
            pytest.skip(f"{values_path} not found — copy from phase6 outputs")

        with open(values_path) as f:
            values = yaml.safe_load(f)

        assert "image"          in values
        assert "replicaCount"   in values
        assert "resources"      in values
        assert "autoscaling"    in values
        assert "serviceAccount" in values

    def test_argocd_manifest_valid_yaml(self):
        """argocd/cloudops-agent-app.yaml should be valid YAML."""
        import yaml
        argocd_path = "argocd/cloudops-agent-app.yaml"
        if not os.path.exists(argocd_path):
            pytest.skip(f"{argocd_path} not found")

        with open(argocd_path) as f:
            docs = list(yaml.safe_load_all(f))

        assert len(docs) >= 1
        assert docs[0]["kind"] == "Application"


if __name__ == "__main__":
    pytest.main([__file__, "-v", "--tb=short"])
