"""Lambda function for generating a presigned URL for uploading a recording to S3."""

from typing import Any
from uuid import uuid4
import json

from botocore.client import Config
import boto3


def lambda_handler(*args: Any, **kwargs: Any):
    """Main entry point for the lambda function."""
    return {
        "statusCode": 200,
        "body": json.dumps(get_presigned_url()),
    }


def get_presigned_url():
    """Generate a presigned URL for uploading a recording to S3."""
    bucket = "openadapt"
    region_name = "us-east-1"
    s3 = boto3.client(
        "s3",
        config=Config(signature_version="s3v4"),
        region_name=region_name,
        endpoint_url=f"https://s3.{region_name}.amazonaws.com",
    )
    key = f"recordings/{uuid4()}.zip"

    presigned_url = s3.generate_presigned_url(
        ClientMethod="put_object",
        Params={
            "Bucket": bucket,
            "Key": key,
        },
        ExpiresIn=3600,
    )

    return {"url": presigned_url}
