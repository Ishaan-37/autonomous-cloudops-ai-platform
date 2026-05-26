# test_imports.py
# Run with: python test_imports.py

print("Testing imports...")

# ── LangGraph ─────────────────────────────────────────────────
from langgraph.graph import StateGraph, END
from langgraph.checkpoint.memory import MemorySaver
from langgraph.types import interrupt
print("✅ LangGraph OK")

# ── LangChain ─────────────────────────────────────────────────
from langchain_openai import ChatOpenAI
from langchain_core.messages import HumanMessage, SystemMessage
print("✅ LangChain OK")

# ── OpenAI SDK ────────────────────────────────────────────────
from openai import AsyncOpenAI
print("✅ OpenAI SDK OK")

# ── Pydantic ──────────────────────────────────────────────────
from pydantic import BaseModel, Field
from pydantic_settings import BaseSettings
print("✅ Pydantic OK")

# ── AWS SDK ───────────────────────────────────────────────────
import boto3
print("✅ boto3 OK")

# ── Pinecone ──────────────────────────────────────────────────
from pinecone import Pinecone
print("✅ Pinecone OK")

# ── Slack ─────────────────────────────────────────────────────
from slack_sdk.web.async_client import AsyncWebClient
print("✅ Slack SDK OK")

# ── FastAPI ───────────────────────────────────────────────────
from fastapi import FastAPI, BackgroundTasks, Request
from fastapi.responses import JSONResponse
print("✅ FastAPI OK")

# ── Agent State (your own code) ───────────────────────────────
from agent.state import AgentState, initial_state
print("✅ agent.state OK")

# ── Agent Graph (your own code) ───────────────────────────────
from agent.graph import build_agent_graph
print("✅ agent.graph OK")

# ── All Nodes ─────────────────────────────────────────────────
from agent.nodes.ingest_node    import ingest_node
from agent.nodes.analyze_node   import analyze_node
from agent.nodes.plan_node      import plan_node
from agent.nodes.approval_node  import approval_node
from agent.nodes.remediate_node import remediate_node
from agent.nodes.report_node    import report_node
print("✅ All 6 nodes OK")

# ── Graph wires up correctly ──────────────────────────────────
memory = MemorySaver()
graph  = build_agent_graph(checkpointer=memory)
print("✅ Graph compiled OK")

print("\n🚀 ALL IMPORTS PASSED — your architecture is wired correctly.")
print("   Ready for Phase 5: FastAPI backend.")