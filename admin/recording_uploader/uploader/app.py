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
    handler = handlers.get(lambda_function)
    if not handler:
        return {
            "statusCode": 400,
            "body": json.dumps(
                {"error": f"Unknown lambda function: {lambda_function}"}
            ),
        }
    return handler(data)


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
    s3 = boto3.client(
        "s3",
        config=Config(signature_version="s3v4"),
        region_name=DEFAULT_REGION_NAME,
        endpoint_url=f"https://s3.{DEFAULT_REGION_NAME}.amazonaws.com",
    )

    presigned_url = s3.generate_presigned_url(
        ClientMethod=client_method,
        Params={
            "Bucket": DEFAULT_BUCKET,
            "Key": key,
        },
        ExpiresIn=ONE_HOUR_IN_SECONDS,
    )

    return {
        "statusCode": 200,
        "body": json.dumps({"url": presigned_url}),
    }


def delete_object(data: dict) -> dict:
    """Delete an object from the s3 bucket

    Args:
        data (dict): The data from the request.

    Returns:
        dict: A dictionary containing the deleted status
    """
    try:
        key = data["key"]
    except Exception as e:
        print(e)
        return {
            "statusCode": 400,
            "body": json.dumps(
                {"error": "Missing 'key' or 'client_method' in request body."}
            ),
        }

    s3 = boto3.client(
        "s3",
        config=Config(signature_version="s3v4"),
        region_name=DEFAULT_REGION_NAME,
        endpoint_url=f"https://s3.{DEFAULT_REGION_NAME}.amazonaws.com",
    )
    s3.delete_object(
        Bucket=DEFAULT_BUCKET,
        Key=key,
    )
    return {"statusCode": 200, "body": json.dumps({"message": "Deleted"})}


handlers = {"get_presigned_url": get_presigned_url, "delete_object": delete_object}
