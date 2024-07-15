"""Lambda function for generating a presigned URL for uploading a recording to S3."""

from typing import Any
from uuid import uuid4
import json

from botocore.client import Config
import boto3

DEFAULT_REGION_NAME = "us-east-1"
DEFAULT_BUCKET = "openadapt"
ONE_HOUR_IN_SECONDS = 3600


def lambda_handler(event: dict, context: Any) -> dict:
    """Main entry point for the lambda function."""
    try:
        user_id = json.loads(event["body"])["user_id"]
    except Exception as e:
        print(e)
        return {
            "statusCode": 400,
            "body": json.dumps({"error": "Missing 'user_id' in request body."}),
        }
    return {
        "statusCode": 200,
        "body": json.dumps(get_presigned_url(user_id)),
    }


def get_presigned_url(
    user_id: str, bucket: str = DEFAULT_BUCKET, region_name: str = DEFAULT_REGION_NAME
) -> dict:
    """Generate a presigned URL for uploading a recording to S3.

    Args:
        bucket (str): The S3 bucket to upload the recording to.
        region_name (str): The AWS region the bucket is in.

    Returns:
        dict: A dictionary containing the presigned URL.
    """
    s3 = boto3.client(
        "s3",
        config=Config(signature_version="s3v4"),
        region_name=region_name,
        endpoint_url=f"https://s3.{region_name}.amazonaws.com",
    )
    key = f"recordings/{user_id}/{uuid4()}.zip"

    presigned_url = s3.generate_presigned_url(
        ClientMethod="put_object",
        Params={
            "Bucket": bucket,
            "Key": key,
        },
        ExpiresIn=ONE_HOUR_IN_SECONDS,
    )

    return {"url": presigned_url}
