"""
agent/graph.py
--------------
THE CONDUCTOR. WIRES ALL 6 NODES INTO A DIRECTED GRAPH.

This is the most important file in the agent. It defines:
  1. Which nodes exist
  2. What order they run in
  3. Which conditional branches exist
  4. Where the graph pauses for human input (interrupt)

GRAPH STRUCTURE:
                    ┌─────────────┐
  alarm arrives ──► │ ingest_node │  fetches logs + RAG context
                    └──────┬──────┘
                           │
                    ┌──────▼──────┐
                    │analyze_node │  GPT-4 root cause analysis
                    └──────┬──────┘
                           │
                    ┌──────▼──────┐
                    │  plan_node  │  generates fix plan
                    └──────┬──────┘
                           │
              ┌────────────▼────────────┐
              │   route_by_action_type  │  conditional router
              └────┬────────┬───────────┘
                   │        │              │
            auto_safe  auto_risky     manual_only / no_action
                   │        │              │
                   │  ┌─────▼──────┐      │
                   │  │approval_   │      │
                   │  │node        │      │
                   │  │(INTERRUPT) │      │
                   │  └─────┬──────┘      │
                   │        │             │
                   └────────┤             │
                    ┌───────▼──────┐      │
                    │remediate_node│      │
                    └───────┬──────┘      │
                            └─────────────┘
                                  │
                    ┌─────────────▼──────┐
                    │   report_node      │  always runs last
                    └─────────────┬──────┘
                                  │
                                 END

CHECKPOINTER:
  LangGraph needs a checkpointer to support the interrupt in approval_node.
  We use SqliteSaver for local development and MemorySaver for tests.
  In production on EKS, switch to PostgresSaver (requires a Postgres DB).

THREAD CONFIG:
  Each agent run has a unique thread_id (= run_id).
  The checkpointer saves state per thread_id.
  This is what allows approval_node to PAUSE and RESUME correctly.
"""

import logging
import uuid
from typing import Literal

from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph import StateGraph, END
from langgraph.types import interrupt

from agent.state import AgentState, initial_state
from agent.nodes.ingest_node     import ingest_node
from agent.nodes.analyze_node    import analyze_node
from agent.nodes.plan_node       import plan_node
from agent.nodes.approval_node   import approval_node
from agent.nodes.remediate_node  import remediate_node
from agent.nodes.report_node     import report_node

logger = logging.getLogger(__name__)


# ── Conditional Router ────────────────────────────────────────
def route_by_action_type(
    state: AgentState,
) -> Literal["approval_node", "remediate_node", "report_node"]:
    """
    After plan_node runs, decide which path to take:

    auto_safe   → go straight to remediate_node (no approval needed)
    auto_risky  → go to approval_node (pause for human input)
    manual_only → go straight to report_node (no automation)
    no_action   → go straight to report_node (nothing to do)
    """
    fix_plan    = state.get("fix_plan", {})
    action_type = fix_plan.get("action_type", "manual_only") if fix_plan else "manual_only"

    logger.info(f"[{state.get('run_id')}] Routing by action_type: {action_type}")

    if action_type == "auto_safe":
        return "remediate_node"
    elif action_type == "auto_risky":
        return "approval_node"
    else:
        # manual_only, no_action, or unknown → skip to report
        return "report_node"


def route_after_approval(
    state: AgentState,
) -> Literal["remediate_node", "report_node"]:
    """
    After the human clicks a button:
    - Approved → remediate_node
    - Rejected → report_node (skip remediation)
    """
    if state.get("approved", False):
        return "remediate_node"
    else:
        return "report_node"


# ── Build the Graph ───────────────────────────────────────────
def build_agent_graph(checkpointer=None):
    """
    Construct and compile the LangGraph agent graph.

    Args:
        checkpointer: LangGraph checkpointer for state persistence.
                      Required for interrupt (approval) to work.
                      Use SqliteSaver for local, MemorySaver for tests.

    Returns:
        Compiled LangGraph graph ready to call .ainvoke() on.
    """
    graph = StateGraph(AgentState)

    # ── Register all nodes ────────────────────────────────────
    graph.add_node("ingest_node",    ingest_node)
    graph.add_node("analyze_node",   analyze_node)
    graph.add_node("plan_node",      plan_node)
    graph.add_node("approval_node",  approval_node)
    graph.add_node("remediate_node", remediate_node)
    graph.add_node("report_node",    report_node)

    # ── Define the edges (the arrows in the diagram) ──────────
    graph.set_entry_point("ingest_node")

    # Linear edges — always go from A to B
    graph.add_edge("ingest_node",  "analyze_node")
    graph.add_edge("analyze_node", "plan_node")

    # Conditional edge after plan_node
    # route_by_action_type() decides which node comes next
    graph.add_conditional_edges(
        "plan_node",
        route_by_action_type,
        {
            "remediate_node": "remediate_node",
            "approval_node":  "approval_node",
            "report_node":    "report_node",
        }
    )

    # Conditional edge after approval_node
    # route_after_approval() decides: remediate or skip?
    graph.add_conditional_edges(
        "approval_node",
        route_after_approval,
        {
            "remediate_node": "remediate_node",
            "report_node":    "report_node",
        }
    )

    # Both remediate_node and report_node end the same way
    graph.add_edge("remediate_node", "report_node")
    graph.add_edge("report_node",    END)

    # ── Compile ───────────────────────────────────────────────
    # interrupt_before=["approval_node"] tells LangGraph:
    # "When you reach approval_node, call interrupt() inside it"
    # The checkpointer MUST be provided for this to work.
    compiled = graph.compile(
        checkpointer=checkpointer,
        interrupt_before=["approval_node"] if checkpointer else [],
    )

    logger.info("Agent graph compiled successfully")
    return compiled


# ── Graph singleton with SQLite checkpointer ─────────────────
# This is what FastAPI imports and calls.
# SQLite file stores all run states — survives server restarts.
_checkpointer = InMemorySaver()
agent = build_agent_graph(checkpointer=_checkpointer)


# ── Public API ────────────────────────────────────────────────

async def run_agent(alert_payload: dict, run_id=None) -> dict:
    """
    Entry point called by FastAPI when a CloudWatch alarm arrives.

    Creates a new state, invokes the graph, and returns the final state.
    The graph may pause at approval_node — that's handled by resume_agent().

    Args:
        alert_payload: raw CloudWatch/SNS alarm payload dict

    Returns:
        Final AgentState dict after the run completes (or pauses)
    """
    run_id = run_id or str(uuid.uuid4())
    state  = initial_state(alert=alert_payload, run_id=run_id)

    # thread config = how LangGraph tracks this specific run
    config = {"configurable": {"thread_id": run_id}}

    logger.info(f"Starting agent run: {run_id}")

    try:
        result = await agent.ainvoke(state, config=config)
        logger.info(f"Agent run complete: {run_id}")
        return result
    except Exception as e:
        logger.error(f"Agent run failed: {run_id} — {e}", exc_info=True)
        return {"run_id": run_id, "errors": [str(e)]}


async def resume_agent(run_id: str, human_decision: dict) -> dict:
    """
    Resume a paused agent run after human clicks Approve/Reject in Slack.

    Called by the FastAPI /webhook/slack/actions endpoint.

    Args:
        run_id:         The run_id from the Slack button value JSON
        human_decision: {"approved": True} or {"rejected": True}

    Returns:
        Final AgentState after the resumed run completes
    """
    config = {"configurable": {"thread_id": run_id}}

    logger.info(f"Resuming agent run: {run_id} with decision: {human_decision}")

    try:
        # Update the state with the human's decision
        # This is what approval_node's interrupt() returns when resumed
        await agent.aupdate_state(
            config,
            human_decision,
            as_node="approval_node"
        )

        # Resume the graph from where it paused
        result = await agent.ainvoke(None, config=config)
        logger.info(f"Agent run resumed and completed: {run_id}")
        return result

    except Exception as e:
        logger.error(f"Failed to resume agent run {run_id}: {e}", exc_info=True)
        return {"run_id": run_id, "errors": [str(e)]}


def get_run_state(run_id: str) -> dict:
    """Get the current state of a run (useful for debugging and status checks)."""
    config = {"configurable": {"thread_id": run_id}}
    snapshot = agent.get_state(config)
    return snapshot.values if snapshot else {}
