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


def retrieve_context(query, top_k=3):

    embedding = create_embedding(query)

    results = index.query(
        vector=embedding,
        top_k=top_k,
        include_metadata=True
    )

    matches = results["matches"]

    context = []

    for match in matches:
        if "text" in match["metadata"]:
            context.append(match["metadata"]["text"])

    return "\n".join(context)


def analyze_issue(query, context):

    print("\n========== AI DIAGNOSIS ==========\n")

    if "CloudWatch" in query:

        print("Possible causes:")
        print("- IAM permissions missing")
        print("- CloudWatch agent not installed")
        print("- Fluent Bit daemonset issue")
        print("- Log group misconfiguration")

        print("\nSuggested actions:")
        print("1. Verify CloudWatch IAM policies")
        print("2. Check aws-for-fluent-bit pods")
        print("3. Inspect log group existence")
        print("4. Verify node role permissions")

    elif "EKS" in query:

        print("Possible causes:")
        print("- Worker node IAM role issue")
        print("- CNI plugin issue")
        print("- Node bootstrap failure")
        print("- Security group issue")

        print("\nSuggested actions:")
        print("1. Verify node IAM role")
        print("2. Check VPC CNI status")
        print("3. Verify subnet routing")
        print("4. Check worker node logs")

    else:

        print("General cloud infrastructure issue detected.")
        print("Manual investigation recommended.")

    print("\n========== RETRIEVED CONTEXT ==========\n")

    chunks = context.split("\n")

    for chunk in chunks[:10]:

        cleaned = chunk.strip()

        if len(cleaned) > 80:
            print(cleaned)
            print("\n----------------------\n")


if __name__ == "__main__":

    query = input("Describe cloud issue: ")

    context = retrieve_context(query)

    analyze_issue(query, context)