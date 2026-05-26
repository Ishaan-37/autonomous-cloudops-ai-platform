import boto3
import time
import os
import random
from pinecone import Pinecone
from dotenv import load_dotenv

load_dotenv()

# AWS CLIENT
logs_client = boto3.client(
    "logs",
    region_name=os.getenv("AWS_REGION")
)

# PINECONE
pc = Pinecone(api_key=os.getenv("PINECONE_API_KEY"))

index = pc.Index(os.getenv("LIVE_LOG_INDEX"))


# TEMP FAKE EMBEDDING
def create_embedding(text):
    return [random.random() for _ in range(1536)]


LOG_GROUP = "/aws/eks/cloudops-staging-cluster/cluster"

def ingest_logs():

    print("Starting CloudWatch ingestion...")

    streams = logs_client.describe_log_streams(
        logGroupName=LOG_GROUP,
        orderBy="LastEventTime",
        descending=True,
        limit=5
    )

    for stream in streams["logStreams"]:

        stream_name = stream["logStreamName"]

        print(f"\nReading stream: {stream_name}")

        events = logs_client.get_log_events(
            logGroupName=LOG_GROUP,
            logStreamName=stream_name,
            limit=20
        )

        for event in events["events"]:

            message = event["message"]

            embedding = create_embedding(message)

            vector_id = str(time.time()) + str(random.randint(0, 99999))

            index.upsert(
                vectors=[
                    {
                        "id": vector_id,
                        "values": embedding,
                        "metadata": {
                            "text": message,
                            "source": "cloudwatch"
                        }
                    }
                ]
            )

            print("Stored log:", message[:80])


if __name__ == "__main__":

    ingest_logs()