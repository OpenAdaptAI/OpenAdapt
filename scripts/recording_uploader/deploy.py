"""Entrypoint to deploy the uploader to AWS Lambda."""


import pathlib
import subprocess

from loguru import logger
import boto3
import fire

CURRENT_DIR = pathlib.Path(__file__).parent


def main(region_name: str = "us-east-1", guided: bool = True) -> None:
    """Deploy the uploader to AWS Lambda.

    Args:
        region_name (str): The AWS region to deploy the Lambda function to.
        guided (bool): Whether to use the guided SAM deployment.
    """
    s3 = boto3.client(
        "s3",
        region_name=region_name,
        endpoint_url=f"https://s3.{region_name}.amazonaws.com",
    )
    bucket = "openadapt"

    # create the S3 bucket, if it doesn't already exist
    try:
        s3.create_bucket(
            ACL="private",
            Bucket=bucket,
        )
    except (s3.exceptions.BucketAlreadyExists, s3.exceptions.BucketAlreadyOwnedByYou):
        proceed = input(f"Bucket '{bucket}' already exists. Proceed? [y/N] ")
        if proceed.lower() != "y":
            return

    # deploy the code to AWS Lambda
    commands = ["sam", "deploy"]
    if guided:
        commands.append("--guided")
    subprocess.run(commands, cwd=CURRENT_DIR, check=True)
    logger.info("Lambda function deployed successfully.")


if __name__ == "__main__":
    fire.Fire(main)
