import requests
from bs4 import BeautifulSoup
from pinecone import Pinecone
from dotenv import load_dotenv
import os
from embedder import create_embedding
import uuid

load_dotenv("../.env")

pc = Pinecone(
    api_key=os.getenv("PINECONE_API_KEY")
)

index = pc.Index("aws-docs")

AWS_DOC_URLS = [
    "https://docs.aws.amazon.com/eks/latest/userguide/troubleshooting.html",
    "https://docs.aws.amazon.com/AmazonCloudWatch/latest/logs/WhatIsCloudWatchLogs.html",
    "https://docs.aws.amazon.com/eks/latest/userguide/create-node-role.html"
]

def chunk_text(text, chunk_size=500):

    chunks = []

    for i in range(0, len(text), chunk_size):
        chunks.append(text[i:i+chunk_size])

    return chunks

for url in AWS_DOC_URLS:

    print(f"Ingesting: {url}")

    response = requests.get(url)

    soup = BeautifulSoup(response.text, "html.parser")

    text = soup.get_text()

    chunks = chunk_text(text)

    vectors = []

    for chunk in chunks[:20]:

        embedding = create_embedding(chunk)

        vectors.append({
            "id": str(uuid.uuid4()),
            "values": embedding,
            "metadata": {
                "text": chunk,
                "source": url
            }
        })

    index.upsert(vectors=vectors)

print("AWS docs ingestion completed.")