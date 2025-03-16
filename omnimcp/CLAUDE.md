# OmniMCP Development Notes

**FOCUS: GET THIS WORKING ASAP**

⚠️ **CRITICAL RULES** ⚠️
- NEVER VIEW the contents of any .env file
- NEVER ASK to see the contents of any .env file
- NEVER SUGGEST viewing the contents of any .env file
- These files contain sensitive credentials that must remain private
- ALWAYS USE --auto-deploy-parser when running OmniMCP
- NEVER USE --allow-no-parser under any circumstances

## Installation Commands

```bash
# Install OmniMCP with minimal dependencies
./install.sh

# Install additional dependencies for OmniParser deployment
# For temporary use (doesn't modify pyproject.toml):
uv pip install paramiko

# For permanent addition (modifies pyproject.toml):
# uv add paramiko
```

## AWS Configuration for OmniParser

OmniParser deployment requires AWS credentials. These need to be set in OpenAdapt's deploy module:

```bash
# Copy the deploy example file to the actual .env file
cp /Users/abrichr/oa/src/OpenAdapt/deploy/.env.example /Users/abrichr/oa/src/OpenAdapt/deploy/.env

# Edit the .env file to add your AWS credentials
# AWS_ACCESS_KEY_ID, AWS_SECRET_ACCESS_KEY, and AWS_REGION must be set
```

### Important Deployment Fixes

If OmniParser deployment fails, check for these common issues:

1. **Correct import path**: The correct import path in `omnimcp/adapters/omniparser.py` should be:
   ```python
   from deploy.deploy.models.omniparser.deploy import Deploy
   ```

2. **AWS Region**: Make sure to use a region where your AWS account has a properly configured default VPC with subnets. For example:
   ```
   AWS_REGION=us-east-1
   ```

3. **VPC Subnet issue**: If you encounter "No subnets found in VPC" error, the deploy script has been modified to automatically create a subnet in your default VPC.

4. **Key pair path**: The EC2 key pair is now stored in the deployment script directory to avoid permission issues.

5. **Remote URL connection**: OmniMCP now captures the EC2 instance's public IP address and updates the OmniParser client URL to connect to the remote server instead of localhost.

6. **Deployment time**: OmniParser deployment timeline:
   - First-time container build: ~5 minutes (includes downloading models)
   - Server ready time: ~1 minute after container starts
   - Subsequent connections: Should be near-instantaneous (< 1 second)

**TODO:** Implement functionality to override the .env file location to allow keeping credentials in the omnimcp directory.

## Running OmniMCP

```bash
# Run in debug mode with auto-deploy OmniParser (no confirmation)
omnimcp debug --auto-deploy-parser --skip-confirmation

# Run in CLI mode with auto-deploy OmniParser (no confirmation)
omnimcp cli --auto-deploy-parser --skip-confirmation

# Run as MCP server with auto-deploy OmniParser (no confirmation)
omnimcp server --auto-deploy-parser --skip-confirmation

# Always use auto-deploy with skip-confirmation for best results
# DO NOT use --allow-no-parser as it provides limited functionality
```

## Managing OmniParser EC2 Instances

```bash
# To stop an OmniParser EC2 instance (prevents additional AWS charges)
cd /Users/abrichr/oa/src/OpenAdapt/deploy
uv python deploy/models/omniparser/deploy.py stop
```

## OmniMCP Testing Plan

### 1. Installation
- Navigate to the omnimcp directory
- Run the installation script
- Verify that omnimcp is available in PATH

### 2. Debug Mode
- Run omnimcp in debug mode without auto-deploy-parser
- Verify that it takes a screenshot and attempts to analyze UI elements
- Save the debug visualization

### 3. OmniParser Deployment (if AWS credentials are available)
- Run omnimcp with auto-deploy-parser flag
- Verify that it deploys OmniParser to AWS EC2
- Check the deployment status and get the server URL

### 4. CLI Mode
- Run omnimcp in CLI mode with the server URL from previous step
- Test simple commands like 'find the close button'
- Verify that it can analyze the screen and take actions

### 5. MCP Server Mode
- Run omnimcp in server mode
- Test connection with Claude Desktop (if available)
- Verify that Claude can use the MCP tools

### 6. Computer Use Mode
- Run the computer-use command (if Docker is available)
- Verify that it launches the Anthropic Computer Use container
- Test browser access to the web interfaces

### 7. Cleanup
- Stop any running OmniParser instances on AWS
- Clean up any temporary files