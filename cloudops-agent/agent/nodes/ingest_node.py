"""
agent/nodes/ingest_node.py
--------------------------
NODE 1 OF 6 IN THE AGENT GRAPH.

PURPOSE:
  When an alarm fires, the agent needs CONTEXT before it can analyze anything.
  This node fetches that context from two sources:
    1. Pinecone (cloudops-logs index) — recent logs similar to this alarm
    2. Pinecone (aws-docs index)      — AWS documentation relevant to this alarm
    3. CloudWatch directly            — raw recent logs from the affected resource

WHY FETCH FROM PINECONE INSTEAD OF CLOUDWATCH DIRECTLY?
  CloudWatch gives you raw recent logs.
  Pinecone gives you semantically similar logs from the PAST.

  Example: "High CPU alarm on i-0abc123"
    CloudWatch → last 100 log lines from that instance (recent, but limited)
    Pinecone   → "5 times in the past month, high CPU on this instance
                  was preceded by a memory leak in the app server"

  Combining both = full picture: what's happening NOW + what happened BEFORE.

WHAT THIS NODE RETURNS (added to AgentState):
  - logs: list of raw log chunks
  - log_context: formatted string ready to paste into LLM prompt
  - doc_context: formatted AWS docs ready to paste into LLM prompt
  - node_history: ["ingest_node"] appended
"""

import logging
from datetime import datetime, timedelta, timezone

import boto3

from agent.state import AgentState
from rag.embedder import query_logs, query_docs, format_rag_context

logger = logging.getLogger(__name__)

cw_logs = boto3.client("logs")


async def ingest_node(state: AgentState) -> dict:
    """
    Fetch log context and RAG context for the incoming alarm.

    Input from state:  alert (the CloudWatch alarm payload)
    Output to state:   logs, log_context, doc_context, node_history
    """
    logger.info(f"[{state['run_id']}] ingest_node started")
    alert = state["alert"]

    # ── Build a search query from the alarm ───────────────────
    # We use the alarm name + reason as the semantic search query.
    # This finds past logs that are SIMILAR in meaning to this alarm.
    alarm_name   = alert.get("AlarmName", "")
    state_reason = alert.get("StateReason", "")
    search_query = f"{alarm_name} {state_reason}"

    logger.info(f"[{state['run_id']}] Searching Pinecone with: '{search_query[:80]}...'")

    # ── Query 1: Similar past logs ────────────────────────────
    try:
        log_results = await query_logs(
            query_text=search_query,
            top_k=5,   # Top 5 most similar past log events
        )
        logger.info(f"[{state['run_id']}] Found {len(log_results)} similar past log events")
    except Exception as e:
        logger.error(f"[{state['run_id']}] Pinecone log query failed: {e}")
        log_results = []

    # ── Query 2: Relevant AWS documentation ───────────────────
    try:
        doc_results = await query_docs(
            query_text=search_query,
            top_k=3,   # Top 3 most relevant doc sections
        )
        logger.info(f"[{state['run_id']}] Found {len(doc_results)} relevant doc sections")
    except Exception as e:
        logger.error(f"[{state['run_id']}] Pinecone doc query failed: {e}")
        doc_results = []

    # ── Also fetch LIVE logs directly from CloudWatch ─────────
    # This catches logs from the LAST 15 MINUTES that may not be
    # in Pinecone yet (ingestion runs every 60 seconds)
    live_logs = await fetch_live_logs(alert)

    # ── Format everything into LLM-ready strings ──────────────
    rag_context  = format_rag_context(log_results, doc_results)
    live_context = format_live_logs(live_logs)

    # Combine: live logs first (most recent), then historical context
    full_log_context = f"""
=== LIVE LOGS (last 15 minutes) ===
{live_context}

{rag_context}
""".strip()

    logger.info(f"[{state['run_id']}] ingest_node complete. Context length: {len(full_log_context)} chars")

    return {
        "logs":         log_results + live_logs,
        "log_context":  full_log_context,
        "doc_context":  format_doc_context(doc_results),
        "node_history": ["ingest_node"],
        "errors":       [] if (log_results or live_logs) else ["ingest_node: no logs found"],
    }


async def fetch_live_logs(alert: dict) -> list[dict]:
    """
    Fetch the most recent logs directly from CloudWatch.
    Used for logs too recent to be in Pinecone yet.

    Tries to identify which log group to query from the alarm metadata.
    Falls back to querying all monitored groups.
    """
    # Try to extract the log group from alarm dimensions
    dimensions = alert.get("Trigger", {}).get("Dimensions", [])
    instance_id = next(
        (d["value"] for d in dimensions if d.get("name") == "InstanceId"),
        None
    )

    # Look in the last 15 minutes only (Pinecone has older data)
    start_ms = int(
        (datetime.now(tz=timezone.utc) - timedelta(minutes=15))
        .timestamp() * 1000
    )

    live_events = []
    log_group = "/cloudops/staging/app-logs"  # Default log group

    try:
        filter_pattern = "?ERROR ?WARN ?FATAL ?OOM ?timeout ?refused"
        if instance_id:
            filter_pattern = f"{filter_pattern} ?{instance_id}"

        paginator = cw_logs.get_paginator("filter_log_events")
        pages = paginator.paginate(
            logGroupName=log_group,
            startTime=start_ms,
            filterPattern=filter_pattern,
            PaginationConfig={"MaxItems": 200}
        )

        for page in pages:
            for event in page.get("events", []):
                live_events.append({
                    "text":      event["message"],
                    "timestamp": datetime.fromtimestamp(
                        event["timestamp"] / 1000, tz=timezone.utc
                    ).isoformat(),
                    "source":    "live-cloudwatch",
                    "score":     1.0   # Live logs are maximally relevant
                })

    except Exception as e:
        logger.warning(f"Could not fetch live logs: {e}")

    return live_events[:50]  # Cap at 50 most recent live events


def format_live_logs(live_logs: list[dict]) -> str:
    """Format live CloudWatch logs into a readable string for the LLM."""
    if not live_logs:
        return "No live log events found in the last 15 minutes."

    lines = []
    for log in live_logs[:20]:  # Show top 20 in the prompt
        lines.append(f"[{log.get('timestamp', 'unknown')}] {log.get('text', '')[:300]}")

    return "\n".join(lines)


def format_doc_context(doc_results: list[dict]) -> str:
    """Format AWS doc results for the LLM context."""
    if not doc_results:
        return "No relevant AWS documentation found."

    lines = ["=== AWS DOCUMENTATION ==="]
    for r in doc_results:
        lines.append(f"\n[{r.get('section', 'AWS Docs')}]")
        lines.append(r.get("text", "")[:800])
        lines.append(f"Source: {r.get('source', '')}")

    return "\n".join(lines)
