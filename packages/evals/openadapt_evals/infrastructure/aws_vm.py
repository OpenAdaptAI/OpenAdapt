"""AWS VM operations for WAA benchmark evaluation.

Provides a Python API for EC2 instance lifecycle management using boto3.
Implements the VMProvider protocol so PoolManager can use AWS identically
to Azure.

Requires: pip install boto3  (or: uv sync --extra aws)

Auth uses boto3's default credential chain (first match wins):
    1. Environment variables (AWS_ACCESS_KEY_ID + AWS_SECRET_ACCESS_KEY)
    2. IAM Identity Center / SSO (~/.aws/config with sso_session)
    3. Shared credentials file (~/.aws/credentials)
    4. AWS config file (~/.aws/config)
    5. Instance metadata (on EC2)

SSO is the recommended approach for interactive use:
    aws configure sso        # one-time setup
    aws sso login            # opens browser, caches token
    # Then all oa-vm --cloud aws commands work automatically

Example:
    from openadapt_evals.infrastructure.aws_vm import AWSVMManager

    vm = AWSVMManager(region="us-east-1")
    info = vm.create_vm("waa-pool-00", region="us-east-1", size="m5.2xlarge")
    ip = vm.get_vm_ip("waa-pool-00")
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger(__name__)

# Instance types with nested virtualization (KVM) support.
# WAA requires QEMU/KVM. As of late 2025, Intel Xeon 6 families (C8i, M8i, R8i)
# support nested virtualization on standard (non-metal) instances, dramatically
# reducing cost vs legacy metal instances.
INSTANCE_TYPE = "m8i.2xlarge"  # 8 vCPU, 32GB, ~$0.46/hr — nested virt supported
INSTANCE_TYPE_FALLBACKS = [
    ("m8i.2xlarge", 0.461),    # 8 vCPU, 32GB — best value with nested virt
    ("c8i.2xlarge", 0.408),    # 8 vCPU, 16GB — compute-optimized, cheaper
    ("r8i.2xlarge", 0.604),    # 8 vCPU, 64GB — memory-optimized
    ("m8i.4xlarge", 0.922),    # 16 vCPU, 64GB — bigger option
    ("m5.metal", 4.608),       # Legacy: 96 vCPU, 384GB — expensive but proven
    ("m5a.xlarge", 0.172),     # Non-KVM fallback (testing only, won't run QEMU)
]

# GPU instance types for verl-agent RL training.
# verl-agent requires 2+ GPUs for distributed VLM training.
# p3.8xlarge: 4x V100 16GB NVLink — recommended for Qwen2.5-VL-3B training.
# g5.12xlarge: 4x A10G 24GB — budget option (no NVLink).
# p3.2xlarge: 1x V100 16GB — single-GPU baseline (tight for 3B).
GPU_INSTANCE_TYPE_FALLBACKS = [
    # Prefer Ampere (g5) over Volta (p3) — the AWS Deep Learning OSS NVIDIA
    # Driver AMI uses the open-source nvidia.ko which requires GPU System
    # Processor (GSP), only available on Turing (T4) and newer architectures.
    # V100 (Volta/p3) will fail with "not supported by open nvidia.ko".
    ("g5.xlarge", 1.006),       # 1x A10G 24GB — cheapest Ampere option
    ("g5.2xlarge", 1.212),      # 1x A10G 24GB, more CPU/RAM
    ("g5.12xlarge", 7.09),      # 4x A10G 96GB — multi-GPU training
    ("p3.2xlarge", 3.06),       # 1x V100 16GB — requires proprietary driver
]

# Regions to try in order of preference
AWS_REGIONS = ["us-east-1", "us-west-2", "us-east-2", "eu-west-1"]

# Ubuntu 22.04 LTS AMI name pattern (Canonical official)
_UBUNTU_AMI_NAME = "ubuntu/images/hvm-ssd/ubuntu-jammy-22.04-amd64-server-*"
_UBUNTU_AMI_OWNER = "099720109477"  # Canonical

# AWS Deep Learning AMI (Ubuntu 22.04, NVIDIA OSS driver pre-installed)
_DL_AMI_NAME = "Deep Learning OSS Nvidia Driver AMI GPU PyTorch *Ubuntu 22.04*"
_DL_AMI_OWNER = "898082745236"  # Amazon (AWS Deep Learning team)

# Tag used to identify pool resources
_POOL_TAG_KEY = "waa-pool"


def _default_region() -> str:
    """Get default AWS region from config or environment."""
    try:
        from openadapt_evals.config import settings

        return settings.aws_region
    except Exception:
        import os

        return os.environ.get("AWS_DEFAULT_REGION", "us-east-1")


@dataclass
class AWSVMManager:
    """Manages AWS EC2 instances for WAA benchmark evaluation.

    Uses boto3 for all AWS operations.

    Args:
        region: AWS region for instance operations.
    """

    region: str = field(default_factory=_default_region)

    def __post_init__(self) -> None:
        self._ec2_client = None
        self._ec2_resource = None

    def _get_ec2_client(self):
        """Lazy-load boto3 EC2 client."""
        if self._ec2_client is None:
            import boto3

            self._ec2_client = boto3.client("ec2", region_name=self.region)
        return self._ec2_client

    def _get_ec2_resource(self):
        """Lazy-load boto3 EC2 resource."""
        if self._ec2_resource is None:
            import boto3

            self._ec2_resource = boto3.resource("ec2", region_name=self.region)
        return self._ec2_resource

    @property
    def resource_scope(self) -> str:
        """Cloud-agnostic resource scope (AWS region)."""
        return self.region

    @property
    def ssh_username(self) -> str:
        """Default SSH username for Ubuntu EC2 instances."""
        return "ubuntu"

    def _find_instance_by_name(self, name: str) -> dict | None:
        """Find a running/stopped instance by its Name tag."""
        ec2 = self._get_ec2_client()
        resp = ec2.describe_instances(
            Filters=[
                {"Name": "tag:Name", "Values": [name]},
                {
                    "Name": "instance-state-name",
                    "Values": ["running", "stopped", "pending", "stopping"],
                },
            ]
        )
        for reservation in resp.get("Reservations", []):
            for instance in reservation.get("Instances", []):
                return instance
        return None

    def _find_latest_ubuntu_ami(self, region: str | None = None) -> str:
        """Find the latest Ubuntu 22.04 LTS AMI in the given region."""
        import boto3

        ec2 = boto3.client("ec2", region_name=region or self.region)
        resp = ec2.describe_images(
            Owners=[_UBUNTU_AMI_OWNER],
            Filters=[
                {"Name": "name", "Values": [_UBUNTU_AMI_NAME]},
                {"Name": "architecture", "Values": ["x86_64"]},
                {"Name": "state", "Values": ["available"]},
            ],
        )
        images = sorted(
            resp["Images"], key=lambda x: x["CreationDate"], reverse=True
        )
        if not images:
            raise RuntimeError(
                f"No Ubuntu 22.04 AMI found in {region or self.region}"
            )
        return images[0]["ImageId"]

    def _find_latest_dl_ami(self, region: str | None = None) -> str:
        """Find the latest AWS Deep Learning AMI (Ubuntu 22.04, NVIDIA OSS driver).

        This AMI comes with NVIDIA drivers, CUDA, and PyTorch pre-installed,
        suitable for GPU training workloads. Falls back to the standard Ubuntu
        AMI if no Deep Learning AMI is found in the region.
        """
        import boto3

        ec2 = boto3.client("ec2", region_name=region or self.region)
        resp = ec2.describe_images(
            Owners=[_DL_AMI_OWNER],
            Filters=[
                {"Name": "name", "Values": [_DL_AMI_NAME]},
                {"Name": "architecture", "Values": ["x86_64"]},
                {"Name": "state", "Values": ["available"]},
            ],
        )
        images = sorted(
            resp["Images"], key=lambda x: x["CreationDate"], reverse=True
        )
        if not images:
            logger.warning(
                "No Deep Learning AMI found in %s, falling back to Ubuntu",
                region or self.region,
            )
            return self._find_latest_ubuntu_ami(region)
        logger.info("Using Deep Learning AMI: %s", images[0]["Name"])
        return images[0]["ImageId"]

    def _ensure_vpc_infrastructure(self, region: str | None = None) -> dict:
        """Ensure VPC, subnet, security group, and key pair exist.

        Creates them if they don't exist, using tags for idempotent lookup.

        Returns:
            Dict with keys: subnet_id, security_group_id, key_name.
        """
        import boto3

        ec2 = boto3.client("ec2", region_name=region or self.region)
        ec2_resource = boto3.resource("ec2", region_name=region or self.region)
        tag_name = "waa-pool-infra"

        # Find or create VPC
        vpcs = ec2.describe_vpcs(
            Filters=[{"Name": "tag:Name", "Values": [tag_name]}]
        )["Vpcs"]
        if vpcs:
            vpc_id = vpcs[0]["VpcId"]
        else:
            vpc = ec2_resource.create_vpc(CidrBlock="10.0.0.0/16")
            vpc.create_tags(Tags=[{"Key": "Name", "Value": tag_name}])
            vpc.wait_until_available()
            # Enable DNS
            ec2.modify_vpc_attribute(VpcId=vpc.id, EnableDnsHostnames={"Value": True})
            ec2.modify_vpc_attribute(VpcId=vpc.id, EnableDnsSupport={"Value": True})
            vpc_id = vpc.id

        # Find or create internet gateway
        igws = ec2.describe_internet_gateways(
            Filters=[{"Name": "tag:Name", "Values": [tag_name]}]
        )["InternetGateways"]
        if igws:
            igw_id = igws[0]["InternetGatewayId"]
        else:
            igw = ec2_resource.create_internet_gateway()
            igw.create_tags(Tags=[{"Key": "Name", "Value": tag_name}])
            igw.attach_to_vpc(VpcId=vpc_id)
            igw_id = igw.id

        # Find or create subnet
        subnets = ec2.describe_subnets(
            Filters=[
                {"Name": "vpc-id", "Values": [vpc_id]},
                {"Name": "tag:Name", "Values": [tag_name]},
            ]
        )["Subnets"]
        if subnets:
            subnet_id = subnets[0]["SubnetId"]
        else:
            subnet = ec2_resource.create_subnet(
                VpcId=vpc_id, CidrBlock="10.0.1.0/24"
            )
            subnet.create_tags(Tags=[{"Key": "Name", "Value": tag_name}])
            # Auto-assign public IPs
            ec2.modify_subnet_attribute(
                SubnetId=subnet.id,
                MapPublicIpOnLaunch={"Value": True},
            )
            subnet_id = subnet.id

        # Ensure route table has internet route
        route_tables = ec2.describe_route_tables(
            Filters=[{"Name": "vpc-id", "Values": [vpc_id]}]
        )["RouteTables"]
        for rt in route_tables:
            has_igw_route = any(
                r.get("GatewayId", "").startswith("igw-")
                for r in rt.get("Routes", [])
            )
            if not has_igw_route:
                try:
                    ec2.create_route(
                        RouteTableId=rt["RouteTableId"],
                        DestinationCidrBlock="0.0.0.0/0",
                        GatewayId=igw_id,
                    )
                except Exception:
                    pass  # Route may already exist

        # Find or create security group
        sgs = ec2.describe_security_groups(
            Filters=[
                {"Name": "vpc-id", "Values": [vpc_id]},
                {"Name": "group-name", "Values": [tag_name]},
            ]
        )["SecurityGroups"]
        if sgs:
            sg_id = sgs[0]["GroupId"]
        else:
            sg = ec2_resource.create_security_group(
                GroupName=tag_name,
                Description="WAA pool security group",
                VpcId=vpc_id,
            )
            # TODO: restrict to user's IP (e.g., via https://checkip.amazonaws.com)
            # Open to 0.0.0.0/0 for now; key-based auth mitigates brute-force risk.
            sg.authorize_ingress(
                IpPermissions=[
                    {
                        "IpProtocol": "tcp",
                        "FromPort": 22,
                        "ToPort": 22,
                        "IpRanges": [{"CidrIp": "0.0.0.0/0", "Description": "SSH"}],
                    },
                ]
            )
            sg_id = sg.id

        # Import SSH key pair
        key_name = "waa-pool-key"
        try:
            ec2.describe_key_pairs(KeyNames=[key_name])
        except Exception as e:
            if "InvalidKeyPair.NotFound" not in str(e):
                raise
            ssh_pub_key_path = Path.home() / ".ssh" / "id_rsa.pub"
            if not ssh_pub_key_path.exists():
                raise RuntimeError(
                    f"SSH public key not found at {ssh_pub_key_path}. "
                    "Run: ssh-keygen -t rsa -b 4096"
                )
            ec2.import_key_pair(
                KeyName=key_name,
                PublicKeyMaterial=ssh_pub_key_path.read_bytes(),
            )

        return {
            "subnet_id": subnet_id,
            "security_group_id": sg_id,
            "key_name": key_name,
        }

    # =========================================================================
    # Public API (VMProvider protocol)
    # =========================================================================

    def get_vm_ip(self, name: str) -> Optional[str]:
        """Get EC2 instance public IP address."""
        instance = self._find_instance_by_name(name)
        if instance:
            return instance.get("PublicIpAddress")
        return None

    def get_vm_state(self, name: str) -> Optional[str]:
        """Get EC2 instance state."""
        instance = self._find_instance_by_name(name)
        if instance:
            state = instance["State"]["Name"]
            # Map to Azure-like display status for compatibility
            state_map = {
                "running": "VM running",
                "stopped": "VM deallocated",
                "pending": "VM starting",
                "stopping": "VM stopping",
                "terminated": "VM deleted",
            }
            return state_map.get(state, state)
        return None

    def create_vm(
        self,
        name: str,
        region: str,
        size: str,
        image: str = "",
        admin_username: str = "ubuntu",
        image_id: str | None = None,
        gpu: bool = False,
        spot: bool = False,
    ) -> dict[str, Any]:
        """Create an EC2 instance.

        Args:
            name: Instance name (set as Name tag).
            region: AWS region.
            size: EC2 instance type (e.g., "m5.2xlarge").
            image: Unused (kept for protocol compatibility).
            admin_username: SSH username (default: "ubuntu").
            image_id: AMI ID. If None, auto-selected based on gpu flag.
            gpu: If True, use the Deep Learning AMI with pre-installed
                NVIDIA drivers and CUDA. Required for GPU training.
            spot: If True, request a spot instance for cost savings.
                The instance will use one-time spot pricing and terminate
                on interruption. See docs/spot_instance_analysis.md for
                pricing details and interruption risk.

        Returns:
            Dict with at least "publicIpAddress" key.

        Raises:
            RuntimeError: If instance creation fails.
        """

        # Update manager's region so subsequent operations find this instance
        if region != self.region:
            logger.info(f"Switching region from {self.region} to {region}")
            self.region = region
            self._ec2_client = None
            self._ec2_resource = None

        ec2_client = self._get_ec2_client()
        ec2_resource = self._get_ec2_resource()

        try:
            infra = self._ensure_vpc_infrastructure(region)
            if image_id:
                ami_id = image_id
            elif gpu:
                ami_id = self._find_latest_dl_ami(region)
            else:
                ami_id = self._find_latest_ubuntu_ami(region)

            # Build create_instances kwargs
            create_kwargs: dict[str, Any] = dict(
                ImageId=ami_id,
                InstanceType=size,
                KeyName=infra["key_name"],
                MinCount=1,
                MaxCount=1,
                NetworkInterfaces=[
                    {
                        "DeviceIndex": 0,
                        "SubnetId": infra["subnet_id"],
                        "Groups": [infra["security_group_id"]],
                        "AssociatePublicIpAddress": True,
                    }
                ],
                BlockDeviceMappings=[
                    {
                        "DeviceName": "/dev/sda1",
                        "Ebs": {
                            "VolumeSize": 128,
                            "VolumeType": "gp3",
                            "DeleteOnTermination": True,
                        },
                    }
                ],
                TagSpecifications=[
                    {
                        "ResourceType": "instance",
                        "Tags": [
                            {"Key": "Name", "Value": name},
                            {"Key": _POOL_TAG_KEY, "Value": "true"},
                        ],
                    }
                ],
            )

            if spot:
                logger.info(
                    "Requesting spot instance for %s (%s)", name, size
                )
                create_kwargs["InstanceMarketOptions"] = {
                    "MarketType": "spot",
                    "SpotOptions": {
                        "SpotInstanceType": "one-time",
                        "InstanceInterruptionBehavior": "terminate",
                    },
                }

            # Launch instance
            instances = ec2_resource.create_instances(**create_kwargs)

            instance = instances[0]
            logger.info(f"Instance {name} ({instance.id}) launching...")

            # Wait for running state
            instance.wait_until_running()
            instance.reload()

            public_ip = instance.public_ip_address
            if not public_ip:
                # Allocate and associate an Elastic IP
                eip = ec2_client.allocate_address(Domain="vpc")
                ec2_client.associate_address(
                    InstanceId=instance.id,
                    AllocationId=eip["AllocationId"],
                )
                ec2_client.create_tags(
                    Resources=[eip["AllocationId"]],
                    Tags=[
                        {"Key": "Name", "Value": f"{name}-eip"},
                        {"Key": _POOL_TAG_KEY, "Value": "true"},
                    ],
                )
                public_ip = eip["PublicIp"]

            spot_label = " (spot)" if spot else ""
            logger.info(f"Instance {name}{spot_label} running at {public_ip}")
            return {"publicIpAddress": public_ip, "name": name, "spot": spot}

        except Exception as e:
            raise RuntimeError(f"EC2 instance creation failed: {e}") from e

    def delete_vm(self, name: str) -> bool:
        """Terminate EC2 instance and release associated Elastic IP.

        Waits for the instance to reach 'terminated' state before returning.
        """
        try:
            instance = self._find_instance_by_name(name)
            if not instance:
                logger.debug(f"Instance {name} not found")
                return True  # Already gone

            instance_id = instance["InstanceId"]
            ec2 = self._get_ec2_client()

            # Find and release any Elastic IPs
            addresses = ec2.describe_addresses(
                Filters=[
                    {"Name": "instance-id", "Values": [instance_id]},
                ]
            ).get("Addresses", [])
            for addr in addresses:
                if addr.get("AssociationId"):
                    ec2.disassociate_address(AssociationId=addr["AssociationId"])
                ec2.release_address(AllocationId=addr["AllocationId"])

            # Terminate instance
            ec2.terminate_instances(InstanceIds=[instance_id])
            logger.info(f"Terminating instance {name} ({instance_id}), waiting...")

            waiter = ec2.get_waiter("instance_terminated")
            waiter.wait(InstanceIds=[instance_id])
            logger.info(f"Instance {name} terminated")
            return True

        except Exception as e:
            logger.error(f"Failed to delete instance {name}: {e}")
            return False

    def deallocate_vm(self, name: str) -> bool:
        """Stop an EC2 instance (equivalent to Azure deallocate).

        Waits for the instance to reach 'stopped' state before returning,
        so that a subsequent start_vm() call won't hit IncorrectInstanceState.
        """
        try:
            instance = self._find_instance_by_name(name)
            if not instance:
                return False

            ec2 = self._get_ec2_client()
            instance_id = instance["InstanceId"]
            ec2.stop_instances(InstanceIds=[instance_id])
            logger.info(f"Stopping instance {name}, waiting for stopped state...")

            waiter = ec2.get_waiter("instance_stopped")
            waiter.wait(InstanceIds=[instance_id])
            logger.info(f"Instance {name} is now stopped")
            return True

        except Exception as e:
            logger.error(f"Failed to stop instance {name}: {e}")
            return False

    def start_vm(self, name: str) -> bool:
        """Start a stopped EC2 instance."""
        try:
            instance = self._find_instance_by_name(name)
            if not instance:
                return False

            ec2 = self._get_ec2_client()
            ec2.start_instances(InstanceIds=[instance["InstanceId"]])
            logger.info(f"Starting instance {name}")

            # Wait for running and get new IP
            waiter = ec2.get_waiter("instance_running")
            waiter.wait(InstanceIds=[instance["InstanceId"]])
            return True

        except Exception as e:
            logger.error(f"Failed to start instance {name}: {e}")
            return False

    def set_auto_shutdown(self, name: str, hours: int = 4) -> bool:
        """Schedule instance shutdown via user-data script.

        v1 implementation: uses `at` or `shutdown` command via SSH.
        A more robust approach would use CloudWatch Events + Lambda.
        """
        try:
            from openadapt_evals.infrastructure.azure_vm import ssh_run

            ip = self.get_vm_ip(name)
            if not ip:
                return False

            minutes = hours * 60
            proc = ssh_run(
                ip,
                f"sudo shutdown -h +{minutes}",
                username=self.ssh_username,
            )
            # `ssh_run` does not raise on a non-zero exit. Returning True
            # without checking asserted that the cost safety net was armed when
            # it may not have been -- and the instance then bills until someone
            # notices.
            returncode = getattr(proc, "returncode", None)
            if returncode not in (0, None):
                logger.error(
                    "Auto-shutdown NOT set for %s: `shutdown` exited %s: %s",
                    name,
                    returncode,
                    (getattr(proc, "stderr", "") or "").strip()[:300],
                )
                return False
            logger.info(f"Auto-shutdown set for {name} in {hours} hours")
            return True

        except Exception as e:
            logger.warning(f"Failed to set auto-shutdown for {name}: {e}")
            return False

    def find_available_size_and_region(
        self, gpu: bool = False,
    ) -> tuple[str, str, float]:
        """Find a working EC2 instance type and region.

        Checks instance type availability in each region.

        Args:
            gpu: If True, try GPU instances (for verl-agent training).
                Otherwise try CPU/metal instances (for WAA evaluation).

        Returns:
            Tuple of (instance_type, region, cost_per_hour).

        Raises:
            RuntimeError: If no available instance type/region found.
        """
        import boto3

        fallbacks = GPU_INSTANCE_TYPE_FALLBACKS if gpu else INSTANCE_TYPE_FALLBACKS
        for instance_type, cost in fallbacks:
            for region in AWS_REGIONS:
                try:
                    ec2 = boto3.client("ec2", region_name=region)
                    resp = ec2.describe_instance_type_offerings(
                        LocationType="region",
                        Filters=[
                            {"Name": "instance-type", "Values": [instance_type]},
                        ],
                    )
                    if resp.get("InstanceTypeOfferings"):
                        return (instance_type, region, cost)
                except Exception:
                    continue

        raise RuntimeError(
            "No available EC2 instance type/region found. "
            "Check your AWS quotas and region access."
        )

    def list_pool_resources(self, prefix: str = "waa-pool") -> dict[str, list[str]]:
        """List EC2 resources matching a pool prefix.

        Args:
            prefix: Resource name prefix to match.

        Returns:
            Dict with keys "instances" and "eips" mapping to lists of IDs.
        """
        ec2 = self._get_ec2_client()
        result: dict[str, list[str]] = {"instances": [], "eips": []}

        # Find instances by Name tag
        try:
            resp = ec2.describe_instances(
                Filters=[
                    {"Name": "tag:Name", "Values": [f"{prefix}*"]},
                    {
                        "Name": "instance-state-name",
                        "Values": ["running", "stopped", "pending", "stopping"],
                    },
                ]
            )
            for reservation in resp.get("Reservations", []):
                for instance in reservation.get("Instances", []):
                    name_tag = ""
                    for tag in instance.get("Tags", []):
                        if tag["Key"] == "Name":
                            name_tag = tag["Value"]
                    result["instances"].append(name_tag or instance["InstanceId"])
        except Exception as e:
            logger.debug(f"Failed to list instances: {e}")

        # Find Elastic IPs with pool tag
        try:
            resp = ec2.describe_addresses(
                Filters=[
                    {"Name": f"tag:{_POOL_TAG_KEY}", "Values": ["true"]},
                ]
            )
            for addr in resp.get("Addresses", []):
                name_tag = ""
                for tag in addr.get("Tags", []):
                    if tag["Key"] == "Name":
                        name_tag = tag["Value"]
                result["eips"].append(name_tag or addr["AllocationId"])
        except Exception as e:
            logger.debug(f"Failed to list EIPs: {e}")

        return result

    def cleanup_pool_resources(
        self, prefix: str, resources: dict[str, list[str]]
    ) -> bool:
        """Delete EC2 pool resources.

        Terminates instances first, then releases Elastic IPs.

        Args:
            prefix: Pool name prefix.
            resources: Dict from list_pool_resources().

        Returns:
            True if all resources were cleaned up.
        """
        ec2 = self._get_ec2_client()
        all_ok = True

        # Terminate instances
        for name in resources.get("instances", []):
            try:
                if name.startswith("i-"):
                    # Raw instance ID (no Name tag) — terminate directly
                    ec2.terminate_instances(InstanceIds=[name])
                    logger.info(f"Terminated {name}")
                else:
                    instance = self._find_instance_by_name(name)
                    if instance:
                        ec2.terminate_instances(InstanceIds=[instance["InstanceId"]])
                        logger.info(f"Terminated {name}")
                    else:
                        logger.warning(f"Instance {name} not found, skipping")
            except Exception as e:
                logger.error(f"Failed to terminate {name}: {e}")
                all_ok = False

        # Release Elastic IPs
        for eip_name in resources.get("eips", []):
            try:
                if eip_name.startswith("eipalloc-"):
                    # Raw allocation ID (no Name tag) — look up and release
                    try:
                        addrs = ec2.describe_addresses(AllocationIds=[eip_name])
                        for addr in addrs.get("Addresses", []):
                            if addr.get("AssociationId"):
                                ec2.disassociate_address(
                                    AssociationId=addr["AssociationId"]
                                )
                    except Exception:
                        pass  # May not be associated
                    ec2.release_address(AllocationId=eip_name)
                else:
                    resp = ec2.describe_addresses(
                        Filters=[{"Name": "tag:Name", "Values": [eip_name]}]
                    )
                    for addr in resp.get("Addresses", []):
                        if addr.get("AssociationId"):
                            ec2.disassociate_address(
                                AssociationId=addr["AssociationId"]
                            )
                        ec2.release_address(AllocationId=addr["AllocationId"])
            except Exception as e:
                logger.error(f"Failed to release EIP {eip_name}: {e}")
                all_ok = False

        return all_ok
