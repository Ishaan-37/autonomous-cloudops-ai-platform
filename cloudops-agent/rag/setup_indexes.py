from pinecone import Pinecone, ServerlessSpec
from dotenv import load_dotenv
import os

load_dotenv("../.env")

PINECONE_API_KEY = os.getenv("PINECONE_API_KEY")

pc = Pinecone(api_key=PINECONE_API_KEY)

index_names = pc.list_indexes().names()

if "cloudops-logs" not in index_names:
    pc.create_index(
        name="cloudops-logs",
        dimension=1536,
        metric="cosine",
        spec=ServerlessSpec(
            cloud="aws",
            region="us-east-1"
        )
    )

if "aws-docs" not in index_names:
    pc.create_index(
        name="aws-docs",
        dimension=1536,
        metric="cosine",
        spec=ServerlessSpec(
            cloud="aws",
            region="us-east-1"
        )
    )

print("Pinecone indexes ready.")