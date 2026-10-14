# EC2 Setup Guide for WAA (Windows Agent Arena) Deployment

This guide walks through deploying WAA on AWS EC2 for GUI agent evaluation. WAA runs Windows 11 inside QEMU inside Docker on an Ubuntu EC2 instance, exposing a Flask API for agent interaction.

## Table of Contents

- [Architecture overview](#architecture-overview)
- [Prerequisites](#prerequisites)
- [Path 1: Automated setup with oa-vm CLI](#path-1-automated-setup-with-oa-vm-cli-recommended)
- [Path 2: Manual setup](#path-2-manual-setup)
- [SSH tunnel setup](#ssh-tunnel-setup)
- [Verifying the setup](#verifying-the-setup)
- [Running evaluations](#running-evaluations)
- [Custom task definitions](#custom-task-definitions)
- [Known-state setup](#known-state-setup)
- [Cost estimates](#cost-estimates)
- [Troubleshooting](#troubleshooting)
- [Cleanup](#cleanup)

## Architecture overview

```
LOCAL MACHINE (macOS/Linux)              AWS EC2 (Ubuntu 22.04, m8i.2xlarge)
+---------------------------+            +------------------------------------+
|  oa-vm CLI                | SSH Tunnel |  Docker (waa-auto:latest)          |
|  (pool management)        | --------→ |  +- evaluate_server (:5050)        |
|                           | :5001→5000|  |  +- /evaluate, /setup, /task    |
|  openadapt-evals          | :5050→5051|  +- Samba share (/tmp/smb/)        |
|  (benchmark runner)       | :8006→8006|  +- QEMU (Windows 11)              |
|                           |           |     +- WAA Flask API (:5000)        |
+---------------------------+            |     +- Agent (navi / api-claude)    |
                                         +------------------------------------+
```

Key points:
- **Instance type**: `m8i.2xlarge` is recommended (~$0.46/hr). Intel Xeon 6 families (C8i, M8i, R8i) support nested virtualization on standard (non-metal) instances since late 2025. Legacy metal instances (`m5.metal` at ~$4.61/hr) also work but at ~10x the cost. Older standard instances like `t3.xlarge` do NOT expose `/dev/kvm` and cannot run QEMU.
- **OS**: Ubuntu 22.04 LTS (Canonical official AMI, auto-discovered by the CLI)
- **Ports**: Only SSH (22) is opened in the security group. All other access goes through SSH tunnels.
- **First boot**: ~35 minutes (Windows 11 download + install). Subsequent resumes: ~1-5 minutes.

## Prerequisites

### 1. AWS account and credentials

AWS credentials are resolved via [boto3's default credential chain](https://boto3.amazonaws.com/v1/documentation/api/latest/guide/credentials.html). **SSO (IAM Identity Center) is recommended** for interactive use.

**Option A: SSO (recommended)**

```bash
# One-time setup -- opens a guided wizard
aws configure sso
# Prompts for: SSO start URL, region, account, role name, profile name

# Login (opens browser, caches short-lived token)
aws sso login
```

Example `~/.aws/config` for SSO:

```ini
[default]
sso_session = my-org
sso_account_id = 111122223333
sso_role_name = PowerUserAccess
region = us-east-1

[sso-session my-org]
sso_start_url = https://my-org.awsapps.com/start
sso_region = us-east-1
sso_registration_scopes = sso:account:access
```

**Option B: Static keys (not recommended for interactive use)**

```bash
# In your .env or shell
export AWS_ACCESS_KEY_ID=AKIA...
export AWS_SECRET_ACCESS_KEY=...
export AWS_DEFAULT_REGION=us-east-1
```

### 2. SSH key pair

An RSA key pair at `~/.ssh/id_rsa` and `~/.ssh/id_rsa.pub` is required. The CLI imports your public key into AWS as the `waa-pool-key` key pair.

```bash
# Generate if you don't have one
ssh-keygen -t rsa -b 4096 -f ~/.ssh/id_rsa
```

### 3. Install openadapt-evals

```bash
pip install openadapt-evals[aws]
# or with uv:
uv sync --extra aws
```

This installs `boto3` and the `oa-vm` CLI.

### 4. Verify AWS setup

Run the read-only smoke test to check credentials, SSH key, AMI lookup, instance type availability, and VPC infrastructure:

```bash
oa-vm smoke-test-aws
```

This performs 5 read-only checks:
1. AWS credentials (via `sts.get_caller_identity()`)
2. SSH public key exists at `~/.ssh/id_rsa.pub`
3. Latest Ubuntu 22.04 AMI lookup (Canonical official)
4. `m8i.2xlarge` (or fallback) instance type availability across regions
5. VPC infrastructure (creates VPC, subnet, security group, internet gateway if needed)

For a full lifecycle test (creates and deletes a real EC2 instance, costs ~$0.01):

```bash
oa-vm smoke-test-aws --full
```

### 5. EC2 service quota

`m8i.2xlarge` requires sufficient vCPU quota. By default, new AWS accounts may have limited quotas. To check or request an increase:

1. Go to **AWS Console > Service Quotas > Amazon EC2**
2. Search for "Running On-Demand Standard (A, C, D, H, I, M, R, T, Z) instances"
3. Request a quota increase to at least 8 vCPUs (the `m8i.2xlarge` instance has 8 vCPUs). If using `m5.metal` as a fallback, 96 vCPUs are required.

## Path 1: Automated setup with oa-vm CLI (recommended)

The `oa-vm` CLI automates the entire lifecycle: VM creation, Docker installation, WAA image build, container start, and readiness probing.

### Step 1: Create pool

```bash
# Create a single VM (sufficient for most evaluations)
oa-vm pool-create --cloud aws --workers 1

# Or create multiple VMs for parallel evaluation
oa-vm pool-create --cloud aws --workers 3
```

What happens behind the scenes:
1. Finds an available instance type with nested virt support (tries m8i.2xlarge, c8i.2xlarge, r8i.2xlarge, m8i.4xlarge, m5.metal in order) and region (us-east-1, us-west-2, us-east-2, eu-west-1)
2. Creates VPC infrastructure if needed (VPC, subnet, internet gateway, security group, key pair)
3. Launches Ubuntu 22.04 EC2 instance with 128GB gp3 EBS root volume
4. Waits for SSH to become available
5. Installs Docker, configures persistent storage at `~/docker` (NOT `/mnt` which is ephemeral)
6. Pulls `dockurr/windows:latest` and `windowsarena/winarena:latest` Docker images
7. Uploads the WAA Dockerfile and build context via SCP
8. Builds the `waa-auto:latest` Docker image
9. Installs socat and creates a systemd service for the evaluate server proxy (VM:5051 -> container:5050)
10. Registers the pool in a local JSON registry

### Step 2: Wait for WAA readiness

```bash
# Wait with default 30-minute timeout
oa-vm pool-wait --cloud aws

# Increase timeout for first boot (Windows download + install takes ~35 min)
oa-vm pool-wait --cloud aws --timeout 45
```

This starts the WAA Docker container (with `--device=/dev/kvm` for QEMU) and polls the WAA Flask server at `http://172.30.0.2:5000/probe` inside the container until it responds. On first boot, Windows 11 is downloaded and installed automatically.

### Step 3: Set up SSH tunnels

The automated pool commands handle tunnels internally, but for interactive use you can set them up manually:

```bash
# Get the VM's public IP
VM_IP=$(oa-vm pool-status --cloud aws 2>/dev/null | grep -oP '\d+\.\d+\.\d+\.\d+' | head -1)

# Start SSH tunnels (run in a separate terminal)
ssh -N \
  -L 5001:localhost:5000 \
  -L 5050:localhost:5051 \
  -L 8006:localhost:8006 \
  -o StrictHostKeyChecking=no \
  -o ServerAliveInterval=60 \
  -o ServerAliveCountMax=10 \
  ubuntu@$VM_IP
```

Port mapping:
| Local port | VM port | Service | Description |
|-----------|---------|---------|-------------|
| 5001 | 5000 | WAA Flask API | Agent interaction (screenshot, execute, probe) |
| 5050 | 5051 | Evaluate server | Task setup, evaluation, scoring |
| 8006 | 8006 | noVNC | Browser-based VNC for visual monitoring |

Note: The SSH username for AWS EC2 Ubuntu instances is `ubuntu` (not `azureuser` as on Azure).

### Step 4: Run evaluations

```bash
# Single task
openadapt-evals run --agent api-claude --task notepad_1

# Multiple tasks distributed across pool
oa-vm pool-run --cloud aws --tasks 10

# Full automated flow (create + wait + run)
oa-vm pool-auto --cloud aws --workers 1 --tasks 10 --timeout 45
```

### Step 5: Cleanup (stop billing)

```bash
# Pause (stop VMs, keep disks -- resume later for ~$0.25/day storage)
oa-vm pool-pause --cloud aws

# Resume paused pool (~5 min vs ~42 min for full create)
oa-vm pool-resume --cloud aws

# Delete everything (terminates instances, releases Elastic IPs)
oa-vm pool-cleanup --cloud aws -y
```

## Path 2: Manual setup

For understanding what happens under the hood, or if you need to customize the setup.

### Step 1: Launch EC2 instance

```bash
# Find the latest Ubuntu 22.04 AMI
AMI_ID=$(aws ec2 describe-images \
  --owners 099720109477 \
  --filters "Name=name,Values=ubuntu/images/hvm-ssd/ubuntu-jammy-22.04-amd64-server-*" \
            "Name=architecture,Values=x86_64" \
            "Name=state,Values=available" \
  --query 'sort_by(Images, &CreationDate)[-1].ImageId' \
  --output text \
  --region us-east-1)

echo "Using AMI: $AMI_ID"

# Import your SSH key (if not already done)
aws ec2 import-key-pair \
  --key-name waa-pool-key \
  --public-key-material fileb://~/.ssh/id_rsa.pub \
  --region us-east-1

# Create security group (SSH only)
SG_ID=$(aws ec2 create-security-group \
  --group-name waa-manual \
  --description "WAA manual setup" \
  --query 'GroupId' --output text)

aws ec2 authorize-security-group-ingress \
  --group-id $SG_ID \
  --protocol tcp --port 22 \
  --cidr 0.0.0.0/0

# Launch m8i.2xlarge instance with 128GB disk (nested virt supported)
INSTANCE_ID=$(aws ec2 run-instances \
  --image-id $AMI_ID \
  --instance-type m8i.2xlarge \
  --key-name waa-pool-key \
  --security-group-ids $SG_ID \
  --block-device-mappings '[{"DeviceName":"/dev/sda1","Ebs":{"VolumeSize":128,"VolumeType":"gp3"}}]' \
  --tag-specifications 'ResourceType=instance,Tags=[{Key=Name,Value=waa-manual}]' \
  --query 'Instances[0].InstanceId' \
  --output text \
  --region us-east-1)

echo "Instance: $INSTANCE_ID"

# Wait for running state
aws ec2 wait instance-running --instance-ids $INSTANCE_ID --region us-east-1

# Get public IP
VM_IP=$(aws ec2 describe-instances \
  --instance-ids $INSTANCE_ID \
  --query 'Reservations[0].Instances[0].PublicIpAddress' \
  --output text \
  --region us-east-1)

echo "VM IP: $VM_IP"
```

### Step 2: Install Docker and build WAA image

```bash
# Wait for SSH to become available (may take 1-2 minutes)
until ssh -o StrictHostKeyChecking=no -o ConnectTimeout=5 ubuntu@$VM_IP echo ready 2>/dev/null; do
  sleep 10
done

# Install Docker
ssh ubuntu@$VM_IP "
  sudo apt-get update -qq
  sudo DEBIAN_FRONTEND=noninteractive apt-get install -y -qq docker.io socat
  sudo systemctl start docker
  sudo systemctl enable docker
  sudo usermod -aG docker ubuntu

  # Configure Docker to use persistent storage
  sudo systemctl stop docker
  sudo mkdir -p /home/ubuntu/docker
  echo '{\"data-root\": \"/home/ubuntu/docker\"}' | sudo tee /etc/docker/daemon.json
  sudo systemctl start docker
"

# Pull base images
ssh ubuntu@$VM_IP "
  sudo docker pull dockurr/windows:latest
  sudo docker pull windowsarena/winarena:latest
"

# Upload WAA Dockerfile and build context
# (from the openadapt-evals repo root)
ssh ubuntu@$VM_IP "mkdir -p /tmp/waa-build"
scp openadapt_evals/waa_deploy/Dockerfile ubuntu@$VM_IP:/tmp/waa-build/
scp openadapt_evals/waa_deploy/evaluate_server.py ubuntu@$VM_IP:/tmp/waa-build/
scp openadapt_evals/waa_deploy/start_with_evaluate.sh ubuntu@$VM_IP:/tmp/waa-build/
scp openadapt_evals/waa_deploy/start_waa_server.bat ubuntu@$VM_IP:/tmp/waa-build/
scp openadapt_evals/waa_deploy/api_agent.py ubuntu@$VM_IP:/tmp/waa-build/

# Build the waa-auto image
ssh ubuntu@$VM_IP "sudo docker build -t waa-auto:latest /tmp/waa-build/ && rm -rf /tmp/waa-build"
```

### Step 3: Start WAA container

```bash
ssh ubuntu@$VM_IP "
  # Create persistent storage directory
  mkdir -p /home/ubuntu/waa-storage

  # Run WAA container
  docker run -d --name winarena \
    --device=/dev/kvm \
    --cap-add NET_ADMIN \
    --stop-timeout 120 \
    -p 5000:5000 \
    -p 5050:5050 \
    -p 8006:8006 \
    -p 7200:7200 \
    -v /home/ubuntu/waa-storage:/storage \
    -e VERSION=11e \
    -e RAM_SIZE=8G \
    -e CPU_CORES=4 \
    -e DISK_SIZE=64G \
    -e ARGUMENTS='-qmp tcp:0.0.0.0:7200,server,nowait' \
    waa-auto:latest \
    /entry.sh --prepare-image false --start-client false
"
```

### Step 4: Set up evaluate server proxy

Docker port forwarding for port 5050 is broken by QEMU's `--cap-add NET_ADMIN` tap networking. A socat proxy on the VM host works around this.

```bash
ssh ubuntu@$VM_IP "
  # Create systemd service for evaluate proxy
  sudo tee /etc/systemd/system/socat-waa-evaluate.service > /dev/null << 'EOF'
[Unit]
Description=socat proxy for WAA evaluate endpoint (VM:5051 -> container:5050)
After=docker.service
Requires=docker.service

[Service]
Type=simple
ExecStart=/usr/bin/socat TCP-LISTEN:5051,fork,reuseaddr EXEC:\"docker exec -i winarena socat STDIO TCP:localhost:5050\"
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF

  sudo systemctl daemon-reload
  sudo systemctl enable socat-waa-evaluate.service
  sudo systemctl start socat-waa-evaluate.service
"
```

### Step 5: Wait for Windows to boot

First boot takes approximately 35 minutes (Windows 11 is downloaded and installed automatically). Monitor progress:

```bash
# Watch storage grow (Windows ISO download + install)
ssh ubuntu@$VM_IP "
  while ! docker exec winarena curl -s --max-time 5 http://172.30.0.2:5000/probe 2>/dev/null | grep -q ok; do
    STORAGE=\$(docker exec winarena du -sh /storage/ 2>/dev/null | cut -f1)
    QEMU_UP=\$(docker exec winarena sh -c 'QPID=\$(pgrep -f qemu-system 2>/dev/null | head -1); [ -n \"\$QPID\" ] && ps -o etime= -p \$QPID 2>/dev/null | tr -d \" \" || echo N/A')
    echo \"\$(date +%H:%M:%S) Storage: \$STORAGE | QEMU uptime: \$QEMU_UP\"
    sleep 30
  done
  echo 'WAA server is READY'
"
```

## SSH tunnel setup

Once the WAA server is ready, set up SSH tunnels from your local machine:

```bash
# Start all tunnels in one command
ssh -N \
  -L 5001:localhost:5000 \
  -L 5050:localhost:5051 \
  -L 8006:localhost:8006 \
  -o StrictHostKeyChecking=no \
  -o UserKnownHostsFile=/dev/null \
  -o ServerAliveInterval=60 \
  -o ServerAliveCountMax=10 \
  ubuntu@$VM_IP
```

Or use the built-in tunnel manager:

```python
from openadapt_evals.infrastructure.ssh_tunnel import SSHTunnelManager

manager = SSHTunnelManager()
manager.start_tunnels_for_vm(vm_ip="<VM_IP>", ssh_user="ubuntu")
status = manager.get_tunnel_status()
print(status)
```

## Verifying the setup

With SSH tunnels running:

```bash
# 1. Check WAA Flask API health
curl http://localhost:5001/probe
# Expected: {"status": "ok", ...}

# 2. Check evaluate server health
curl http://localhost:5050/probe
# Expected: {"status": "ok", "service": "evaluate_server"}

# 3. Take a screenshot (returns base64 PNG)
curl -s http://localhost:5001/screenshot | python3 -c "
import sys, json, base64
data = json.load(sys.stdin)
img = base64.b64decode(data['screenshot'])
with open('/tmp/waa_screenshot.png', 'wb') as f:
    f.write(img)
print(f'Screenshot saved: {len(img)} bytes')
"

# 4. Open VNC in browser (visual monitoring)
open http://localhost:8006

# 5. Run the built-in probe command
openadapt-evals probe --server http://localhost:5001

# 6. Run a smoke test with the noop agent (no API key needed)
openadapt-evals run --agent noop --task notepad_1 --server http://localhost:5001
```

## Running evaluations

### With API agents (Claude, GPT)

```bash
# Set your API key
export ANTHROPIC_API_KEY=sk-ant-...
# or
export OPENAI_API_KEY=sk-...

# Run with Claude
openadapt-evals run --agent api-claude --task notepad_1

# Run with GPT-4
openadapt-evals run --agent api-openai --task notepad_1

# Run multiple tasks
openadapt-evals live \
  --agent api-claude \
  --server http://localhost:5001 \
  --task-ids notepad_1,settings_1
```

### View results

```bash
openadapt-evals view --run-name live_eval
# Opens HTML viewer in browser with step-by-step screenshots and action logs
```

## Custom task definitions

WAA tasks are defined as JSON files in the container at `/client/evaluation_examples_windows/examples/<domain>/<task_id>.json`. Each task specifies:

```json
{
  "id": "my-custom-task-WOS",
  "instruction": "Open Notepad and type 'Hello World'",
  "domain": "notepad",
  "snapshot": null,
  "max_steps": 15,
  "related_apps": ["notepad"],
  "evaluator": {
    "func": "check_text_in_notepad",
    "expected": ["Hello World"],
    "result": {
      "type": "exact_match",
      "expected": "Hello World"
    }
  },
  "config": [
    {
      "type": "execute",
      "parameters": {
        "command": "python -c \"import subprocess; subprocess.Popen(['notepad.exe'])\""
      }
    }
  ]
}
```

### Loading custom tasks

**Option A: Via the evaluate server endpoint**

```bash
# Place your JSON file on the VM
scp my_task.json ubuntu@$VM_IP:/tmp/

# Copy into the container
ssh ubuntu@$VM_IP "docker cp /tmp/my_task.json winarena:/client/evaluation_examples_windows/examples/custom/my-task.json"

# Verify it's accessible
curl http://localhost:5050/task/my-task
```

**Option B: Via the Python API**

```python
from openadapt_evals import WAALiveAdapter, WAALiveConfig

adapter = WAALiveAdapter(WAALiveConfig(server_url="http://localhost:5001"))
task = adapter.load_task_from_json("my-task", {
    "instruction": "Open Notepad and type 'Hello World'",
    "domain": "notepad",
    "max_steps": 15,
    "evaluator": {
        "func": "check_text_in_notepad",
        "expected": ["Hello World"],
    },
})
```

## Known-state setup

WAA supports setup handlers that run before each task to establish a known initial state. These are executed via the evaluate server's `/setup` endpoint.

### Verify required apps are installed

```bash
curl -X POST http://localhost:5050/setup \
  -H "Content-Type: application/json" \
  -d '{"config": [{"type": "verify_apps", "parameters": {"apps": ["chrome"]}}]}'
# Returns 200 if present, 422 if missing
```

### Install apps (two-phase pipeline)

Large installers are downloaded on the Linux host first (no timeout), then installed on Windows via Samba share:

```bash
curl -X POST http://localhost:5050/setup \
  -H "Content-Type: application/json" \
  -d '{"config": [{"type": "install_apps", "parameters": {"apps": ["libreoffice-calc"]}}]}'
```

### Execute setup commands on Windows

```bash
# Run a command on the Windows VM via WAA Flask API
curl -X POST http://localhost:5001/execute \
  -H "Content-Type: application/json" \
  -d '{"command": "python -c \"import subprocess; subprocess.Popen(['\''notepad.exe'\''])\""}'

# Clear Chrome browsing data
curl -X POST http://localhost:5001/execute \
  -H "Content-Type: application/json" \
  -d '{"command": "python -c \"import subprocess; subprocess.run(['\''powershell'\'', '\''-Command'\'', '\''Remove-Item -Path \\\"$env:LOCALAPPDATA\\\\Google\\\\Chrome\\\\User Data\\\\Default\\\\*\\\" -Recurse -Force -ErrorAction SilentlyContinue'\''])\""}'
```

### Task setup configs

Tasks can include a `config` array with setup steps that run before the task begins:

```json
{
  "config": [
    {
      "type": "execute",
      "parameters": {
        "command": "python -c \"import subprocess; subprocess.run(['taskkill', '/f', '/im', 'chrome.exe'])\""
      }
    },
    {
      "type": "verify_apps",
      "parameters": {
        "apps": ["chrome"]
      }
    }
  ]
}
```

## Cost estimates

### Instance costs

| Instance type | vCPU | RAM | Cost/hr | KVM support | Notes |
|---------------|------|-----|---------|-------------|-------|
| `m8i.2xlarge` | 8 | 32 GB | $0.46 | Yes | Primary choice (Intel Xeon 6, nested virt) |
| `c8i.2xlarge` | 8 | 16 GB | $0.41 | Yes | Compute-optimized, cheapest with nested virt |
| `r8i.2xlarge` | 8 | 64 GB | $0.60 | Yes | Memory-optimized |
| `m8i.4xlarge` | 16 | 64 GB | $0.92 | Yes | Bigger option |
| `m5.metal` | 96 | 384 GB | $4.61 | Yes | Legacy fallback (expensive) |

### Time and cost per phase

| Phase | Time | Cost (m8i.2xlarge) |
|-------|------|--------------------|
| EC2 launch + SSH ready | ~2 min | $0.02 |
| Docker + image build | ~12 min | $0.09 |
| Windows 11 download + install (first boot) | ~20 min | $0.15 |
| Windows boot (subsequent) | ~1-5 min | $0.01-0.04 |
| **Total first boot** | **~35 min** | **~$0.27** |
| Benchmark runtime | varies | $0.46/hr |

### Storage costs (when paused)

Paused VMs (stopped instances) do not incur compute charges, but EBS storage continues:
- 128 GB gp3 volume: ~$0.24/day ($0.08/GB/month)
- Elastic IP (if not associated): $0.005/hr (~$0.12/day)

## Troubleshooting

### "No available EC2 instance type/region found"

The preferred instance type (`m8i.2xlarge`) may not be available in all regions. The CLI tries multiple instance types (m8i, c8i, r8i, then m5.metal) across regions (us-east-1, us-west-2, us-east-2, eu-west-1) in order. If all fail:
1. Check your vCPU quota: AWS Console > Service Quotas > EC2 > "Running On-Demand Standard instances"
2. Request a quota increase to at least 8 vCPUs (or 96 vCPUs if falling back to m5.metal)
3. Try a different region: `oa-vm smoke-test-aws --region eu-west-1`

### SSH connection timeout

- EC2 instances take 1-2 minutes to become SSH-ready after launch
- Check security group allows inbound TCP port 22
- Verify your SSH key matches: `aws ec2 describe-key-pairs --key-names waa-pool-key`

### Docker build fails

- Ensure 128 GB disk (default 30 GB is too small for Docker images)
- Check Docker data-root is on persistent storage: `docker info | grep "Docker Root Dir"` should show `/home/ubuntu/docker`, NOT `/mnt` (ephemeral)

### WAA server never becomes ready

- First boot takes ~35 minutes. Increase timeout: `oa-vm pool-wait --cloud aws --timeout 45`
- Check container is running: `ssh ubuntu@$VM_IP "docker ps"`
- Check QEMU is running: `ssh ubuntu@$VM_IP "docker exec winarena pgrep -f qemu-system"`
- Check container logs: `ssh ubuntu@$VM_IP "docker logs winarena --tail 50"`
- Monitor Windows install progress by watching storage growth (should go from ~0 to ~8+ GB)

### Port 5050 not accessible

Docker port forwarding for 5050 is broken by QEMU's `--cap-add NET_ADMIN` tap networking. The socat systemd service proxies VM:5051 -> container:5050. Verify it is running:

```bash
ssh ubuntu@$VM_IP "sudo systemctl status socat-waa-evaluate.service"
# If not running:
ssh ubuntu@$VM_IP "sudo systemctl restart socat-waa-evaluate.service"
```

### PyAutoGUI fail-safe (mouse stuck in corner)

If the QEMU VM gets stuck in fail-safe mode (mouse driven to a corner):

```bash
curl -X POST http://localhost:5001/execute \
  -H "Content-Type: application/json" \
  -d '{"command": "python -c \"import pyautogui; pyautogui.FAILSAFE=False; pyautogui.moveTo(500,400)\""}'
```

### NEVER restart the VM for SSH issues

`az vm restart` / stopping and starting the EC2 instance kills the QEMU Windows session, forcing a 35+ minute cold boot. Instead:
- Wait and retry SSH
- Check the instance console log: `aws ec2 get-console-output --instance-id $INSTANCE_ID`
- Use `aws ec2-instance-connect send-ssh-public-key` as a fallback

## Cleanup

### Automated cleanup

```bash
# Delete all pool resources (instances, Elastic IPs, VPC infrastructure)
oa-vm pool-cleanup --cloud aws -y
```

### Manual cleanup

```bash
# Terminate instance
aws ec2 terminate-instances --instance-ids $INSTANCE_ID --region us-east-1

# Wait for termination
aws ec2 wait instance-terminated --instance-ids $INSTANCE_ID --region us-east-1

# Release Elastic IP (if allocated)
aws ec2 release-address --allocation-id $ALLOC_ID --region us-east-1

# Delete security group
aws ec2 delete-security-group --group-id $SG_ID --region us-east-1

# Delete VPC (if you created one manually)
# Must delete subnet and internet gateway first
aws ec2 delete-subnet --subnet-id $SUBNET_ID
aws ec2 detach-internet-gateway --internet-gateway-id $IGW_ID --vpc-id $VPC_ID
aws ec2 delete-internet-gateway --internet-gateway-id $IGW_ID
aws ec2 delete-vpc --vpc-id $VPC_ID
```

### Check for orphaned resources

```bash
# List pool instances
aws ec2 describe-instances \
  --filters "Name=tag:waa-pool,Values=true" "Name=instance-state-name,Values=running,stopped" \
  --query 'Reservations[].Instances[].[InstanceId,Tags[?Key==`Name`].Value|[0],State.Name,PublicIpAddress]' \
  --output table --region us-east-1

# List pool Elastic IPs
aws ec2 describe-addresses \
  --filters "Name=tag:waa-pool,Values=true" \
  --query 'Addresses[].[AllocationId,PublicIp,InstanceId]' \
  --output table --region us-east-1
```
