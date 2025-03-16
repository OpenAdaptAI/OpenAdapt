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