"""Entrypoint to deploy the uploader to AWS Lambda."""

import os
import pathlib
import re
import subprocess

from loguru import logger
import boto3
import fire

CURRENT_DIR = pathlib.Path(__file__).parent


def main(region_name: str = "us-east-1", destroy: bool = False) -> None:
    """Deploy the uploader to AWS Lambda.

    Args:
        region_name (str): The AWS region to deploy the Lambda function to.
        destroy (bool): Whether to delete the Lambda function.
    """
    # check if aws credentials are set
    if os.getenv("AWS_ACCESS_KEY_ID") is None:
        raise ValueError("AWS_ACCESS_KEY_ID is not set")
    if os.getenv("AWS_SECRET_ACCESS_KEY") is None:
        raise ValueError("AWS_SECRET_ACCESS_KEY is not set")
    if destroy:
        commands = ["sam", "delete", "--no-prompts"]
    else:
        s3 = boto3.client(
            "s3",
            region_name=region_name,
            endpoint_url=f"https://s3.{region_name}.amazonaws.com",
        )
        bucket = "openadapt"

        s3.create_bucket(
            ACL="private",
            Bucket=bucket,
        )
        commands = ["sam", "deploy", "--no-fail-on-empty-changeset"]
    try:
        std_kwargs = {}
        if not destroy:
            std_kwargs["stderr"] = subprocess.PIPE
            std_kwargs["stdout"] = subprocess.PIPE
        ret = subprocess.run(
            commands, cwd=CURRENT_DIR, check=True, shell=True, **std_kwargs
        )
        if destroy:
            logger.info("Lambda function deleted successfully.")
        else:
            stdout = ret.stdout.decode("utf-8") if ret.stdout else ""
            # find the url, which is in the format https://${ServerlessRestApi}.execute-api.${AWS::Region}.amazonaws.com/Prod/upload/
            url_match = re.search(
                r"https://([^\.]+)\.execute-api\.([^\.]+)\.amazonaws\.com/Prod/upload/",
                stdout,
            )
            if url_match:
                logger.info(
                    f"Lambda function deployed successfully. URL: {url_match.group(0)},"
                    " copy it to your config."
                )
            else:
                logger.error("Lambda function deployed, but failed to find the URL")
                print(stdout)
    except subprocess.CalledProcessError as e:
        if destroy:
            logger.error("Failed to delete Lambda function")
        else:
            logger.error("Failed to deploy Lambda function")
        raise e


if __name__ == "__main__":
    fire.Fire(main)
