from pinecone import Pinecone
from dotenv import load_dotenv
import os
import random

load_dotenv()

pc = Pinecone(api_key=os.getenv("PINECONE_API_KEY"))

index = pc.Index(os.getenv("AWS_DOCS_INDEX"))


# TEMP fake embedding
def create_embedding(text):
    return [random.random() for _ in range(1536)]


def search_docs(query, top_k=3):
    embedding = create_embedding(query)

    results = index.query(
        vector=embedding,
        top_k=top_k,
        include_metadata=True
    )

    matches = results["matches"]

    print("\nTop Matches:\n")

    for match in matches:
        print(match["metadata"]["text"][:500])
        print("\n-------------------\n")


if __name__ == "__main__":
    query = input("Ask AWS issue: ")
    search_docs(query)