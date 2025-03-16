"""Adapter for interacting with the OmniParser server.

This module provides a client for the OmniParser API deployed on AWS.
"""

import base64
import io
from typing import Dict, List, Any, Optional

from loguru import logger
import requests
from PIL import Image


class OmniParserClient:
    """Client for the OmniParser API."""

    def __init__(self, server_url: str):
        """Initialize the OmniParser client.

        Args:
            server_url: URL of the OmniParser server
        """
        self.server_url = server_url.rstrip("/")  # Remove trailing slash if present
    
    def check_server_available(self) -> bool:
        """Check if the OmniParser server is available.
        
        Returns:
            bool: True if server is available, False otherwise
        """
        try:
            probe_url = f"{self.server_url}/probe/"
            response = requests.get(probe_url, timeout=5)
            response.raise_for_status()
            logger.info("OmniParser server is available")
            return True
        except requests.exceptions.RequestException as e:
            logger.error(f"OmniParser server not available: {e}")
            return False
    
    def image_to_base64(self, image: Image.Image) -> str:
        """Convert a PIL Image to base64 string.
        
        Args:
            image: PIL Image to convert
            
        Returns:
            str: Base64 encoded string of the image
        """
        img_byte_arr = io.BytesIO()
        image.save(img_byte_arr, format='PNG')
        return base64.b64encode(img_byte_arr.getvalue()).decode("utf-8")
    
    def parse_image(self, image: Image.Image) -> Dict[str, Any]:
        """Parse an image using the OmniParser service.
        
        Args:
            image: PIL Image to parse
            
        Returns:
            Dict[str, Any]: Parsed results including UI elements
        """
        if not self.check_server_available():
            return {"error": "Server not available", "parsed_content_list": []}
        
        # Convert image to base64
        base64_image = self.image_to_base64(image)
        
        # Prepare request
        url = f"{self.server_url}/parse/"
        payload = {"base64_image": base64_image}
        
        try:
            # Make request to API
            response = requests.post(url, json=payload, timeout=30)
            response.raise_for_status()
            
            # Parse response
            result = response.json()
            logger.info(f"OmniParser latency: {result.get('latency', 0):.2f} seconds")
            return result
        except requests.exceptions.RequestException as e:
            logger.error(f"Error making request to OmniParser API: {e}")
            return {"error": str(e), "parsed_content_list": []}
        except Exception as e:
            logger.error(f"Error parsing image with OmniParser: {e}")
            return {"error": str(e), "parsed_content_list": []}


class OmniParserProvider:
    """Provider for OmniParser services."""
    
    def __init__(self, server_url: Optional[str] = None):
        """Initialize OmniParser provider.
        
        Args:
            server_url: URL of the OmniParser server (optional)
        """
        self.server_url = server_url or "http://localhost:8000"
        self.client = OmniParserClient(self.server_url)
    
    def is_available(self) -> bool:
        """Check if the OmniParser service is available.
        
        Returns:
            bool: True if service is available, False otherwise
        """
        return self.client.check_server_available()
        
    def status(self) -> Dict[str, Any]:
        """Check the status of the OmniParser service.
        
        Returns:
            Dict[str, Any]: Status information
        """
        is_available = self.is_available()
        return {
            "services": [
                {
                    "name": "omniparser",
                    "status": "running" if is_available else "stopped",
                    "url": self.server_url
                }
            ],
            "is_available": is_available
        }
    
    def deploy(self) -> bool:
        """Deploy the OmniParser service if not already running.
        
        Returns:
            bool: True if successfully deployed or already running, False otherwise
        """
        # First check if there's an existing EC2 instance running OmniParser
        try:
            import boto3
            from deploy.deploy.models.omniparser.deploy import config
            ec2 = boto3.resource("ec2", region_name=config.AWS_REGION)
            instances = ec2.instances.filter(
                Filters=[
                    {"Name": "tag:Name", "Values": [config.PROJECT_NAME]},
                    {"Name": "instance-state-name", "Values": ["running"]},
                ]
            )
            
            # Get the first running instance
            instance = next(iter(instances), None)
            if instance and instance.public_ip_address:
                remote_url = f"http://{instance.public_ip_address}:8000"
                logger.info(f"Found existing OmniParser instance at: {remote_url}")
                
                # Update the client to use the remote URL
                self.server_url = remote_url
                self.client = OmniParserClient(self.server_url)
                
                # Check if the server is responding
                if self.client.check_server_available():
                    logger.info(f"Successfully connected to existing OmniParser server at {remote_url}")
                    return True
                else:
                    logger.info(f"Found existing instance but server not responding at {remote_url}. Will attempt to deploy.")
        except Exception as e:
            logger.warning(f"Error checking for existing EC2 instances: {e}")
        
        # Check if local server is running
        if self.status()["is_available"]:
            logger.info("OmniParser service is already running locally")
            return True
            
        # If we get here, we need to deploy a new instance
        try:
            # The correct import path is deploy.deploy.models.omniparser.deploy
            from deploy.deploy.models.omniparser.deploy import Deploy
            logger.info("Deploying OmniParser service...")
            
            # Modify this class to capture the remote server URL
            class DeployWithUrlCapture(Deploy):
                @staticmethod
                def start():
                    # Get original implementation
                    result = Deploy.start()
                    
                    # Get EC2 instances with matching tags
                    import boto3
                    from deploy.deploy.models.omniparser.deploy import config
                    ec2 = boto3.resource("ec2", region_name=config.AWS_REGION)
                    instances = ec2.instances.filter(
                        Filters=[
                            {"Name": "tag:Name", "Values": [config.PROJECT_NAME]},
                            {"Name": "instance-state-name", "Values": ["running"]},
                        ]
                    )
                    
                    # Get the first running instance
                    instance = next(iter(instances), None)
                    if instance and instance.public_ip_address:
                        return f"http://{instance.public_ip_address}:8000"
                    
                    return result
            
            # Get the remote server URL
            remote_url = DeployWithUrlCapture.start()
            
            # If we got a URL back, update the client to use it
            if isinstance(remote_url, str) and remote_url.startswith("http://"):
                logger.info(f"OmniParser deployed at: {remote_url}")
                self.server_url = remote_url
                self.client = OmniParserClient(self.server_url)
                
                # Verify the server is available
                import time
                
                # Try multiple times to connect to the remote server
                max_retries = 30
                retry_interval = 10
                
                for i in range(max_retries):
                    is_available = self.client.check_server_available()
                    if is_available:
                        logger.info(f"Successfully connected to remote OmniParser server at {remote_url}")
                        return True
                    
                    logger.info(f"Server not ready at {remote_url}. Attempt {i+1}/{max_retries}. Waiting {retry_interval} seconds...")
                    time.sleep(retry_interval)
            
            # Fall back to checking localhost
            return self.status()["is_available"]
        except Exception as e:
            logger.error(f"Failed to deploy OmniParser service: {e}")
            return False
    
    def parse_screenshot(self, image_data: bytes) -> Dict[str, Any]:
        """Parse a screenshot using OmniParser.
        
        Args:
            image_data: Raw image data in bytes
            
        Returns:
            Dict[str, Any]: Parsed content with UI elements
        """
        try:
            image = Image.open(io.BytesIO(image_data))
            return self.client.parse_image(image)
        except Exception as e:
            logger.error(f"Error processing image data: {e}")
            return {"error": str(e), "parsed_content_list": []}