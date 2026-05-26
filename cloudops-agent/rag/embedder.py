import random

def create_embedding(text):
    return [random.random() for _ in range(1536)]
def query_logs(query: str, top_k: int = 5):
    """
    Placeholder log retrieval function.
    Later this will query Pinecone log index.
    """

    return [
        "CloudWatch agent disconnected",
        "IAM permission denied",
        "Node bootstrap timeout"
    ]


def query_docs(query: str, top_k: int = 5):
    """
    Placeholder AWS docs retrieval function.
    Later this will query AWS docs Pinecone index.
    """

    return [
        "EKS worker nodes require IAM permissions.",
        "CloudWatch logging requires proper role bindings.",
        "Security groups must allow cluster communication."
    ]


def format_rag_context(logs, docs):
    """
    Combines logs + docs into one formatted LLM context string.
    """

    context = "\n=== LOGS ===\n"

    for log in logs:
        context += f"- {log}\n"

    context += "\n=== AWS DOCS ===\n"

    for doc in docs:
        context += f"- {doc}\n"

    return context