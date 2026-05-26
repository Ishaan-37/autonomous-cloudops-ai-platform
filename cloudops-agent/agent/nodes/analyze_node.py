from dotenv import load_dotenv
load_dotenv()
"""
agent/nodes/analyze_node.py
----------------------------
NODE 2 OF 6. THE BRAIN OF THE AGENT.

PURPOSE:
  Takes the alarm + log context from ingest_node and asks GPT-4:
  "What is the root cause of this issue?"

  Returns a STRUCTURED analysis with:
  - root_cause:  clear human-readable explanation
  - confidence:  how sure the LLM is (0.0 to 1.0)
  - severity:    CRITICAL | HIGH | MEDIUM | LOW
  - category:    memory | cpu | disk | network | application
  - affected_resources: list of EC2 IDs, pod names, etc.

WHY STRUCTURED OUTPUT?
  If we ask GPT-4 for free text, the next node (plan_node) can't
  reliably parse it. Structured JSON output = each field is guaranteed
  to exist and be the right type. Pydantic validates it.

WHY RAG + LLM TOGETHER?
  LLM alone: "High CPU is usually caused by a runaway process"
    → Generic. Not specific to YOUR infrastructure.

  RAG alone: "3 weeks ago this alarm also fired. Logs show: OOM at 14:32"
    → Historical facts without interpretation.

  RAG + LLM: "Based on past incidents and current logs, the root cause
    is a memory leak in the payment service causing OOM kills, which then
    triggers CPU spikes as the OS tries to recover. Confidence: 0.91"
    → Specific, contextualized, actionable.

SYSTEM PROMPT DESIGN:
  The system prompt is the most important part. It tells the LLM:
  1. Its role (Senior SRE)
  2. What to focus on
  3. Exactly what JSON to output
  4. What NOT to do (hallucinate IDs, be vague, etc.)
"""

import json
import logging
from typing import Optional

from openai import AsyncOpenAI
from pydantic import BaseModel, Field, validator

from agent.state import AgentState, RootCauseAnalysis

logger = logging.getLogger(__name__)
oai = AsyncOpenAI()

# ── System Prompt ─────────────────────────────────────────────
# This is the most critical piece. Spend time tuning this.
RCA_SYSTEM_PROMPT = """
You are a Senior Site Reliability Engineer (SRE) at a cloud-native company.
You are part of an autonomous CloudOps AI agent that monitors AWS infrastructure.

YOUR JOB:
Analyze the CloudWatch alarm and log context provided, then identify the ROOT CAUSE
of the issue. Be specific, concise, and actionable.

RULES:
1. NEVER invent or hallucinate resource IDs, instance IDs, or IP addresses.
   Only reference IDs that appear explicitly in the provided logs or alarm data.
2. If you are uncertain, say so in the root_cause and set confidence below 0.6.
3. Base your analysis PRIMARILY on the provided logs and docs, not general knowledge.
4. Be specific — "memory leak in the payment-service causing OOM kills" is good.
   "The system is experiencing high resource usage" is too vague.
5. severity rules:
   - CRITICAL: service is DOWN or data loss risk
   - HIGH: service is degraded, users impacted
   - MEDIUM: performance degradation, no user impact yet
   - LOW: warning threshold, no immediate action needed

OUTPUT FORMAT:
You MUST respond with ONLY valid JSON. No prose before or after. No markdown.
No explanation. Just the JSON object.

{
  "root_cause": "Clear one-paragraph explanation of what caused the issue",
  "confidence": 0.85,
  "severity": "HIGH",
  "category": "memory",
  "affected_resources": ["i-0abc123def456789"],
  "symptoms": ["OOM kill detected", "Memory at 98%", "Process restarted 3 times"],
  "contributing_factors": ["Memory limit set too low", "Gradual leak over 2 hours"]
}

CATEGORIES: memory | cpu | disk | network | application | database | cost | unknown
""".strip()


# ── Pydantic model for output validation ──────────────────────
class RCAOutput(BaseModel):
    """
    Validates the LLM's JSON output.
    If GPT-4 returns a field with wrong type, Pydantic catches it here
    and we either fix it or fail gracefully — never crash the agent.
    """
    root_cause:           str
    confidence:           float = Field(ge=0.0, le=1.0)
    severity:             str
    category:             str
    affected_resources:   list[str] = []
    symptoms:             list[str] = []
    contributing_factors: list[str] = []

    @validator("severity")
    def validate_severity(cls, v):
        allowed = {"CRITICAL", "HIGH", "MEDIUM", "LOW"}
        v = v.upper()
        return v if v in allowed else "MEDIUM"

    @validator("category")
    def validate_category(cls, v):
        allowed = {"memory", "cpu", "disk", "network", "application",
                   "database", "cost", "unknown"}
        v = v.lower()
        return v if v in allowed else "unknown"

    @validator("confidence")
    def clamp_confidence(cls, v):
        return max(0.0, min(1.0, v))


async def analyze_node(state: AgentState) -> dict:
    """
    Run GPT-4 root cause analysis using alarm + RAG context.

    Input from state:  alert, log_context, doc_context
    Output to state:   root_cause (RootCauseAnalysis), node_history
    """
    logger.info(f"[{state['run_id']}] analyze_node started")

    alert       = state["alert"]
    log_context = state.get("log_context", "No logs available")
    doc_context = state.get("doc_context", "No documentation available")

    # ── Build the user message ────────────────────────────────
    # This is what the LLM actually sees. Structure matters.
    user_message = f"""
CLOUDWATCH ALARM DETAILS:
{json.dumps(alert, indent=2)}

ALARM DESCRIPTION:
Name:   {alert.get('AlarmName', 'Unknown')}
Reason: {alert.get('StateReason', 'Unknown')}
State:  {alert.get('NewStateValue', 'ALARM')}
Region: {alert.get('Region', 'us-east-1')}

{log_context}

{doc_context}

Based on the above alarm and log context, identify the root cause.
Remember: only reference resource IDs that appear in the data above.
Output valid JSON only.
""".strip()

    # ── Call GPT-4 ────────────────────────────────────────────
    try:
        response = await oai.chat.completions.create(
            model="gpt-4o",
            messages=[
                {"role": "system", "content": RCA_SYSTEM_PROMPT},
                {"role": "user",   "content": user_message}
            ],
            response_format={"type": "json_object"},  # Forces JSON output
            temperature=0.1,   # Low temperature = more deterministic, less creative
            max_tokens=1000,
            timeout=30,
        )

        raw_json = response.choices[0].message.content
        logger.info(f"[{state['run_id']}] GPT-4 response: {raw_json[:200]}...")

        # ── Validate and parse the output ─────────────────────
        rca_data = json.loads(raw_json)
        validated = RCAOutput(**rca_data)

        root_cause: RootCauseAnalysis = {
            "root_cause":           validated.root_cause,
            "confidence":           validated.confidence,
            "severity":             validated.severity,
            "category":             validated.category,
            "affected_resources":   validated.affected_resources,
            "symptoms":             validated.symptoms,
            "contributing_factors": validated.contributing_factors,
        }

        logger.info(
            f"[{state['run_id']}] RCA complete — "
            f"severity={validated.severity}, "
            f"confidence={validated.confidence:.2f}, "
            f"category={validated.category}"
        )

        return {
            "root_cause":   root_cause,
            "node_history": ["analyze_node"],
            "errors":       [],
        }

    except json.JSONDecodeError as e:
        logger.error(f"[{state['run_id']}] GPT-4 returned invalid JSON: {e}")
        return {
            "root_cause":   _fallback_rca(alert, str(e)),
            "node_history": ["analyze_node"],
            "errors":       [f"analyze_node: JSON parse error — {e}"],
        }

    except Exception as e:
        logger.error(f"[{state['run_id']}] analyze_node failed: {e}", exc_info=True)
        return {
            "root_cause":   _fallback_rca(alert, str(e)),
            "node_history": ["analyze_node"],
            "errors":       [f"analyze_node: {e}"],
        }


def _fallback_rca(alert: dict, error: str) -> RootCauseAnalysis:
    """
    If GPT-4 fails or returns bad JSON, use this fallback.
    Ensures the agent can keep running even if analysis fails.
    """
    return {
        "root_cause":           f"Analysis failed — manual review required. Alarm: {alert.get('AlarmName')}",
        "confidence":           0.0,
        "severity":             "HIGH",    # Assume HIGH when uncertain — safer
        "category":             "unknown",
        "affected_resources":   [],
        "symptoms":             [alert.get("StateReason", "Unknown")],
        "contributing_factors": [f"LLM analysis error: {error}"],
    }
