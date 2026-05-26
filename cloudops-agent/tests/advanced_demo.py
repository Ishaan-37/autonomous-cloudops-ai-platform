import asyncio
import json
from datetime import datetime

from agent.graph import run_agent


# ─────────────────────────────────────────────────────────────
# REALISTIC CLOUDWATCH ALARM PAYLOAD
# ─────────────────────────────────────────────────────────────

fake_alarm = {
    "AlarmName": "eks-prod-high-cpu",
    "AlarmDescription": "Production EKS CPU usage exceeded threshold",
    "AWSAccountId": "123456789012",
    "NewStateValue": "ALARM",
    "NewStateReason": "Threshold Crossed: CPUUtilization > 90%",
    "StateChangeTime": datetime.utcnow().isoformat(),

    "Region": "ap-south-1",

    "Trigger": {
        "MetricName": "CPUUtilization",
        "Namespace": "AWS/EKS",
        "StatisticType": "Statistic",
        "Statistic": "AVERAGE",
        "Unit": "Percent",
        "Threshold": 90.0,
    },

    "Resources": [
        "arn:aws:eks:ap-south-1:123456789012:cluster/cloudops-prod"
    ]
}


# ─────────────────────────────────────────────────────────────
# PRETTY TERMINAL VISUALS
# ─────────────────────────────────────────────────────────────

def banner(title):
    print("\n" + "=" * 70)
    print(title.center(70))
    print("=" * 70)


def section(title):
    print(f"\n🔹 {title}")
    print("-" * 60)


# ─────────────────────────────────────────────────────────────
# MAIN EXECUTION
# ─────────────────────────────────────────────────────────────

async def main():

    banner("AUTONOMOUS CLOUDOPS AI AGENT")

    section("Incoming CloudWatch Alarm")
    print(json.dumps(fake_alarm, indent=2))

    print("\n🚀 Launching LangGraph orchestration...\n")

    result = await run_agent(fake_alarm)

    banner("FINAL AGENT STATE")

    for key, value in result.items():

        section(key)

        if isinstance(value, (dict, list)):
            print(json.dumps(value, indent=2, default=str))
        else:
            print(value)

    banner("EXECUTION COMPLETE")


asyncio.run(main())