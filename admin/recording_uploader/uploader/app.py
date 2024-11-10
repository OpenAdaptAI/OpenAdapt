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
    data = json.loads(event["body"])
    lambda_function = data["lambda_function"]
    if lambda_function == "get_presigned_url":
        return {
            "body": json.dumps(get_presigned_url(data)),
            "statusCode": 200,
        }
    return {
        "statusCode": 400,
        "body": json.dumps({"error": f"Unknown lambda function: {lambda_function}"}),
    }


def get_presigned_url(data: dict) -> dict:
    """Generate a presigned URL for uploading a recording to S3.

    Args:
        data (dict): The data from the request.

    Returns:
        dict: A dictionary containing the presigned URL.
    """
    try:
        key = data["key"]
        client_method = data["client_method"]
    except Exception as e:
        print(e)
        return {
            "statusCode": 400,
            "body": json.dumps(
                {"error": "Missing 'key' or 'client_method' in request body."}
            ),
        }
    region_name = DEFAULT_REGION_NAME
    bucket = DEFAULT_BUCKET
    s3 = boto3.client(
        "s3",
        config=Config(signature_version="s3v4"),
        region_name=region_name,
        endpoint_url=f"https://s3.{region_name}.amazonaws.com",
    )

    presigned_url = s3.generate_presigned_url(
        ClientMethod=client_method,
        Params={
            "Bucket": bucket,
            "Key": key,
        },
        ExpiresIn=ONE_HOUR_IN_SECONDS,
    )

    return {"url": presigned_url}
