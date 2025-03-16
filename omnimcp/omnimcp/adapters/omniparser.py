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
        # Check if already running
        if self.status()["is_available"]:
            logger.info("OmniParser service is already running")
            return True
            
        # Try to deploy using the deployment script
        try:
            from deploy.deploy.models.omniparser.deploy import Deploy
            logger.info("Deploying OmniParser service...")
            Deploy.start()
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