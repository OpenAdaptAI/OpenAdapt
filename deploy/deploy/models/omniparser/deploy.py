import os
import subprocess
import time

from botocore.exceptions import ClientError
from loguru import logger
from pydantic_settings import BaseSettings
import boto3
import fire
import paramiko

class Config(BaseSettings):
    AWS_ACCESS_KEY_ID: str
    AWS_SECRET_ACCESS_KEY: str
    AWS_REGION: str

    PROJECT_NAME: str = "omniparser"
    REPO_URL: str = "https://github.com/microsoft/OmniParser.git"
    AWS_EC2_AMI: str = "ami-06835d15c4de57810"
    AWS_EC2_DISK_SIZE: int = 128  # GB
    AWS_EC2_INSTANCE_TYPE: str = "g4dn.xlarge"  # (T4 16GB $0.526/hr x86_64)
    AWS_EC2_USER: str = "ubuntu"
    PORT: int = 8000  # FastAPI port
    COMMAND_TIMEOUT: int = 600  # 10 minutes

    class Config:
        env_file = ".env"
        env_file_encoding = 'utf-8'

    @property
    def AWS_EC2_KEY_NAME(self) -> str:
        return f"{self.PROJECT_NAME}-key"

    @property
    def AWS_EC2_KEY_PATH(self) -> str:
        return f"./{self.AWS_EC2_KEY_NAME}.pem"

    @property
    def AWS_EC2_SECURITY_GROUP(self) -> str:
        return f"{self.PROJECT_NAME}-SecurityGroup"

config = Config()

def create_key_pair(key_name: str = config.AWS_EC2_KEY_NAME, key_path: str = config.AWS_EC2_KEY_PATH) -> str | None:
    ec2_client = boto3.client('ec2', region_name=config.AWS_REGION)
    try:
        key_pair = ec2_client.create_key_pair(KeyName=key_name)
        private_key = key_pair['KeyMaterial']

        with open(key_path, "w") as key_file:
            key_file.write(private_key)
        os.chmod(key_path, 0o400)  # Set read-only permissions

        logger.info(f"Key pair {key_name} created and saved to {key_path}")
        return key_name
    except ClientError as e:
        logger.error(f"Error creating key pair: {e}")
        return None

def get_or_create_security_group_id(ports: list[int] = [22, config.PORT]) -> str | None:
    ec2 = boto3.client('ec2', region_name=config.AWS_REGION)

    ip_permissions = [{
        'IpProtocol': 'tcp',
        'FromPort': port,
        'ToPort': port,
        'IpRanges': [{'CidrIp': '0.0.0.0/0'}]
    } for port in ports]

    try:
        response = ec2.describe_security_groups(GroupNames=[config.AWS_EC2_SECURITY_GROUP])
        security_group_id = response['SecurityGroups'][0]['GroupId']
        logger.info(f"Security group '{config.AWS_EC2_SECURITY_GROUP}' already exists: {security_group_id}")

        for ip_permission in ip_permissions:
            try:
                ec2.authorize_security_group_ingress(
                    GroupId=security_group_id,
                    IpPermissions=[ip_permission]
                )
                logger.info(f"Added inbound rule for port {ip_permission['FromPort']}")
            except ClientError as e:
                if e.response['Error']['Code'] == 'InvalidPermission.Duplicate':
                    logger.info(f"Rule for port {ip_permission['FromPort']} already exists")
                else:
                    logger.error(f"Error adding rule for port {ip_permission['FromPort']}: {e}")

        return security_group_id
    except ClientError as e:
        if e.response['Error']['Code'] == 'InvalidGroup.NotFound':
            try:
                response = ec2.create_security_group(
                    GroupName=config.AWS_EC2_SECURITY_GROUP,
                    Description='Security group for OmniParser deployment',
                    TagSpecifications=[
                        {
                            'ResourceType': 'security-group',
                            'Tags': [{'Key': 'Name', 'Value': config.PROJECT_NAME}]
                        }
                    ]
                )
                security_group_id = response['GroupId']
                logger.info(f"Created security group '{config.AWS_EC2_SECURITY_GROUP}' with ID: {security_group_id}")

                ec2.authorize_security_group_ingress(GroupId=security_group_id, IpPermissions=ip_permissions)
                logger.info(f"Added inbound rules for ports {ports}")

                return security_group_id
            except ClientError as e:
                logger.error(f"Error creating security group: {e}")
                return None
        else:
            logger.error(f"Error describing security groups: {e}")
            return None

def deploy_ec2_instance(
    ami: str = config.AWS_EC2_AMI,
    instance_type: str = config.AWS_EC2_INSTANCE_TYPE,
    project_name: str = config.PROJECT_NAME,
    key_name: str = config.AWS_EC2_KEY_NAME,
    disk_size: int = config.AWS_EC2_DISK_SIZE,
) -> tuple[str | None, str | None]:
    ec2 = boto3.resource('ec2')
    ec2_client = boto3.client('ec2')

    # Check for existing instances first
    instances = ec2.instances.filter(
        Filters=[
            {'Name': 'tag:Name', 'Values': [config.PROJECT_NAME]},
            {'Name': 'instance-state-name', 'Values': ['running', 'pending', 'stopped']}
        ]
    )

    existing_instance = None
    for instance in instances:
        existing_instance = instance
        if instance.state['Name'] == 'running':
            logger.info(f"Instance already running: ID - {instance.id}, IP - {instance.public_ip_address}")
            break
        elif instance.state['Name'] == 'stopped':
            logger.info(f"Starting existing stopped instance: ID - {instance.id}")
            ec2_client.start_instances(InstanceIds=[instance.id])
            instance.wait_until_running()
            instance.reload()
            logger.info(f"Instance started: ID - {instance.id}, IP - {instance.public_ip_address}")
            break

    # If we found an existing instance, ensure we have its key
    if existing_instance:
        if not os.path.exists(config.AWS_EC2_KEY_PATH):
            logger.warning(f"Key file {config.AWS_EC2_KEY_PATH} not found for existing instance.")
            logger.warning("You'll need to use the original key file to connect to this instance.")
            logger.warning("Consider terminating the instance with 'deploy.py stop' and starting fresh.")
            return None, None
        return existing_instance.id, existing_instance.public_ip_address

    # No existing instance found, create new one with new key pair
    security_group_id = get_or_create_security_group_id()
    if not security_group_id:
        logger.error("Unable to retrieve security group ID. Instance deployment aborted.")
        return None, None

    # Create new key pair
    try:
        if os.path.exists(config.AWS_EC2_KEY_PATH):
            logger.info(f"Removing existing key file {config.AWS_EC2_KEY_PATH}")
            os.remove(config.AWS_EC2_KEY_PATH)
        
        try:
            ec2_client.delete_key_pair(KeyName=key_name)
            logger.info(f"Deleted existing key pair {key_name}")
        except ClientError:
            pass  # Key pair doesn't exist, which is fine

        if not create_key_pair(key_name):
            logger.error("Failed to create key pair")
            return None, None
    except Exception as e:
        logger.error(f"Error managing key pair: {e}")
        return None, None

    # Create new instance
    ebs_config = {
        'DeviceName': '/dev/sda1',
        'Ebs': {
            'VolumeSize': disk_size,
            'VolumeType': 'gp3',
            'DeleteOnTermination': True
        },
    }

    new_instance = ec2.create_instances(
        ImageId=ami,
        MinCount=1,
        MaxCount=1,
        InstanceType=instance_type,
        KeyName=key_name,
        SecurityGroupIds=[security_group_id],
        BlockDeviceMappings=[ebs_config],
        TagSpecifications=[
            {
                'ResourceType': 'instance',
                'Tags': [{'Key': 'Name', 'Value': project_name}]
            },
        ]
    )[0]

    new_instance.wait_until_running()
    new_instance.reload()
    logger.info(f"New instance created: ID - {new_instance.id}, IP - {new_instance.public_ip_address}")
    return new_instance.id, new_instance.public_ip_address

def configure_ec2_instance(
    instance_id: str | None = None,
    instance_ip: str | None = None,
    max_ssh_retries: int = 20,
    ssh_retry_delay: int = 20,
    max_cmd_retries: int = 20,
    cmd_retry_delay: int = 30,
) -> tuple[str | None, str | None]:
    if not instance_id:
        ec2_instance_id, ec2_instance_ip = deploy_ec2_instance()
    else:
        ec2_instance_id = instance_id
        ec2_instance_ip = instance_ip

    key = paramiko.RSAKey.from_private_key_file(config.AWS_EC2_KEY_PATH)
    ssh_client = paramiko.SSHClient()
    ssh_client.set_missing_host_key_policy(paramiko.AutoAddPolicy())

    ssh_retries = 0
    while ssh_retries < max_ssh_retries:
        try:
            ssh_client.connect(hostname=ec2_instance_ip, username=config.AWS_EC2_USER, pkey=key)
            break
        except Exception as e:
            ssh_retries += 1
            logger.error(f"SSH connection attempt {ssh_retries} failed: {e}")
            if ssh_retries < max_ssh_retries:
                logger.info(f"Retrying SSH connection in {ssh_retry_delay} seconds...")
                time.sleep(ssh_retry_delay)
            else:
                logger.error("Maximum SSH connection attempts reached. Aborting.")
                return None, None

    commands = [
        "sudo apt-get update",
        "sudo apt-get install -y ca-certificates curl gnupg",
        "sudo install -m 0755 -d /etc/apt/keyrings",
        "curl -fsSL https://download.docker.com/linux/ubuntu/gpg | sudo dd of=/etc/apt/keyrings/docker.gpg",
        "sudo chmod a+r /etc/apt/keyrings/docker.gpg",
        'echo "deb [arch="$(dpkg --print-architecture)" signed-by=/etc/apt/keyrings/docker.gpg] https://download.docker.com/linux/ubuntu "$(. /etc/os-release && echo "$VERSION_CODENAME")" stable" | sudo tee /etc/apt/sources.list.d/docker.list > /dev/null',
        "sudo apt-get update",
        "sudo apt-get install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin",
        "sudo systemctl start docker",
        "sudo systemctl enable docker",
        "sudo usermod -a -G docker ${USER}",
        "sudo docker system prune -af --volumes",
        f"sudo docker rm -f {config.PROJECT_NAME}-container || true",
    ]

    for command in commands:
        logger.info(f"Executing command: {command}")
        cmd_retries = 0
        while cmd_retries < max_cmd_retries:
            stdin, stdout, stderr = ssh_client.exec_command(command)
            exit_status = stdout.channel.recv_exit_status()

            if exit_status == 0:
                logger.info(f"Command executed successfully")
                break
            else:
                error_message = stderr.read()
                if "Could not get lock" in str(error_message):
                    cmd_retries += 1
                    logger.warning(f"dpkg is locked, retrying in {cmd_retry_delay} seconds... Attempt {cmd_retries}/{max_cmd_retries}")
                    time.sleep(cmd_retry_delay)
                else:
                    logger.error(f"Error in command: {command}, Exit Status: {exit_status}, Error: {error_message}")
                    break

    ssh_client.close()
    return ec2_instance_id, ec2_instance_ip

class Deploy:
    @staticmethod
    def execute_command(ssh_client: paramiko.SSHClient, command: str) -> None:
        """Execute a command and handle its output safely."""
        logger.info(f"Executing: {command}")
        stdin, stdout, stderr = ssh_client.exec_command(
            command, 
            timeout=config.COMMAND_TIMEOUT,
            # get_pty=True
        )
        
        # Stream output in real-time
        while not stdout.channel.exit_status_ready():
            if stdout.channel.recv_ready():
                try:
                    line = stdout.channel.recv(1024).decode('utf-8', errors='replace')
                    if line.strip():  # Only log non-empty lines
                        logger.info(line.strip())
                except Exception as e:
                    logger.warning(f"Error decoding stdout: {e}")
                    
            if stdout.channel.recv_stderr_ready():
                try:
                    line = stdout.channel.recv_stderr(1024).decode('utf-8', errors='replace')
                    if line.strip():  # Only log non-empty lines
                        logger.error(line.strip())
                except Exception as e:
                    logger.warning(f"Error decoding stderr: {e}")
                    
        exit_status = stdout.channel.recv_exit_status()
        
        # Capture any remaining output
        try:
            remaining_stdout = stdout.read().decode('utf-8', errors='replace')
            if remaining_stdout.strip():
                logger.info(remaining_stdout.strip())
        except Exception as e:
            logger.warning(f"Error decoding remaining stdout: {e}")
            
        try:
            remaining_stderr = stderr.read().decode('utf-8', errors='replace')
            if remaining_stderr.strip():
                logger.error(remaining_stderr.strip())
        except Exception as e:
            logger.warning(f"Error decoding remaining stderr: {e}")
            
        if exit_status != 0:
            error_msg = f"Command failed with exit status {exit_status}: {command}"
            logger.error(error_msg)
            raise RuntimeError(error_msg)
        
        logger.info(f"Successfully executed: {command}")

    @staticmethod
    def start() -> None:
        try:
            instance_id, instance_ip = configure_ec2_instance()
            assert instance_ip, f"invalid {instance_ip=}"

            # Trigger driver installation via login shell
            Deploy.ssh(non_interactive=True)

            # Get the directory containing deploy.py
            current_dir = os.path.dirname(os.path.abspath(__file__))
            
            # Define files to copy
            files_to_copy = {
                "Dockerfile": os.path.join(current_dir, "Dockerfile"),
                ".dockerignore": os.path.join(current_dir, ".dockerignore"),
            }

            # Copy files to instance
            for filename, filepath in files_to_copy.items():
                if os.path.exists(filepath):
                    logger.info(f"Copying {filename} to instance...")
                    subprocess.run([
                        "scp",
                        "-i", config.AWS_EC2_KEY_PATH,
                        "-o", "StrictHostKeyChecking=no",
                        filepath,
                        f"{config.AWS_EC2_USER}@{instance_ip}:~/{filename}"
                    ], check=True)
                else:
                    logger.warning(f"File not found: {filepath}")

            # Connect to instance and execute commands
            key = paramiko.RSAKey.from_private_key_file(config.AWS_EC2_KEY_PATH)
            ssh_client = paramiko.SSHClient()
            ssh_client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
            
            try:
                logger.info(f"Connecting to {instance_ip}...")
                ssh_client.connect(
                    hostname=instance_ip,
                    username=config.AWS_EC2_USER,
                    pkey=key,
                    timeout=30
                )

                setup_commands = [
                    "rm -rf OmniParser",  # Clean up any existing repo
                    f"git clone {config.REPO_URL}",
                    "cp Dockerfile .dockerignore OmniParser/",
                ]

                # Execute setup commands
                for command in setup_commands:
                    logger.info(f"Executing setup command: {command}")
                    Deploy.execute_command(ssh_client, command)

                # Build and run Docker container
                docker_commands = [
                    # Remove any existing container
                    "sudo docker rm -f omniparser-container || true",
                    # Remove any existing image
                    "sudo docker rmi omniparser || true",
                    # Build new image
                    "cd OmniParser && sudo docker build --progress=plain -t omniparser .",
                    # Run new container
                    "sudo docker run -d -p 8000:8000 --gpus all --name omniparser-container omniparser"
                ]

                # Execute Docker commands
                for command in docker_commands:
                    logger.info(f"Executing Docker command: {command}")
                    Deploy.execute_command(ssh_client, command)

                # Wait for container to start and check its logs
                logger.info("Waiting for container to start...")
                time.sleep(10)  # Give container time to start
                Deploy.execute_command(ssh_client, "docker logs omniparser-container")

                # Wait for server to become responsive
                logger.info("Waiting for server to become responsive...")
                max_retries = 30
                retry_delay = 10
                server_ready = False

                for attempt in range(max_retries):
                    try:
                        # Check if server is responding
                        check_command = f"curl -s http://localhost:{config.PORT}/probe/"
                        Deploy.execute_command(ssh_client, check_command)
                        server_ready = True
                        break
                    except Exception as e:
                        logger.warning(f"Server not ready (attempt {attempt + 1}/{max_retries}): {e}")
                        if attempt < max_retries - 1:
                            logger.info(f"Waiting {retry_delay} seconds before next attempt...")
                            time.sleep(retry_delay)

                if not server_ready:
                    raise RuntimeError("Server failed to start properly")

                # Final status check
                Deploy.execute_command(ssh_client, "docker ps | grep omniparser-container")
                
                server_url = f"http://{instance_ip}:{config.PORT}"
                logger.info(f"Deployment complete. Server running at: {server_url}")
                
                # Verify server is accessible from outside
                try:
                    import requests
                    response = requests.get(f"{server_url}/probe/", timeout=10)
                    if response.status_code == 200:
                        logger.info("Server is accessible from outside!")
                    else:
                        logger.warning(f"Server responded with status code: {response.status_code}")
                except Exception as e:
                    logger.warning(f"Could not verify external access: {e}")

            except Exception as e:
                logger.error(f"Error during deployment: {e}")
                # Get container logs for debugging
                try:
                    Deploy.execute_command(ssh_client, "docker logs omniparser-container")
                except:
                    pass
                raise

            finally:
                ssh_client.close()

        except Exception as e:
            logger.error(f"Deployment failed: {e}")
            # Attempt cleanup on failure
            try:
                Deploy.stop()
            except Exception as cleanup_error:
                logger.error(f"Cleanup after failure also failed: {cleanup_error}")
            raise

        logger.info("Deployment completed successfully!")

    @staticmethod
    def status() -> None:
        ec2 = boto3.resource('ec2')
        instances = ec2.instances.filter(
            Filters=[{'Name': 'tag:Name', 'Values': [config.PROJECT_NAME]}]
        )

        for instance in instances:
            public_ip = instance.public_ip_address
            if public_ip:
                server_url = f"http://{public_ip}:{config.PORT}"
                logger.info(f"Instance ID: {instance.id}, State: {instance.state['Name']}, URL: {server_url}")
            else:
                logger.info(f"Instance ID: {instance.id}, State: {instance.state['Name']}, URL: Not available (no public IP)")

    @staticmethod
    def ssh(non_interactive: bool = False) -> None:
        """SSH into the running instance."""
        # Get instance IP
        ec2 = boto3.resource('ec2')
        instances = ec2.instances.filter(
            Filters=[
                {'Name': 'tag:Name', 'Values': [config.PROJECT_NAME]},
                {'Name': 'instance-state-name', 'Values': ['running']}
            ]
        )
        
        instance = next(iter(instances), None)
        if not instance:
            logger.error("No running instance found")
            return
        
        ip = instance.public_ip_address
        if not ip:
            logger.error("Instance has no public IP")
            return
        
        # Check if key file exists
        if not os.path.exists(config.AWS_EC2_KEY_PATH):
            logger.error(f"Key file not found: {config.AWS_EC2_KEY_PATH}")
            return
        
        if non_interactive:
            # Simulate full login by forcing all initialization scripts
            ssh_command = [
                "ssh",
                "-o", "StrictHostKeyChecking=no",  # Automatically accept new host keys
                "-o", "UserKnownHostsFile=/dev/null",  # Prevent writing to known_hosts
                "-i", config.AWS_EC2_KEY_PATH,
                f"{config.AWS_EC2_USER}@{ip}",
                "-t",  # Allocate a pseudo-terminal
                "-tt",  # Force pseudo-terminal allocation
                "bash --login -c 'exit'"  # Force a full login shell and exit immediately
            ]
        else:
            # Build and execute SSH command
            ssh_command = f"ssh -i {config.AWS_EC2_KEY_PATH} -o StrictHostKeyChecking=no {config.AWS_EC2_USER}@{ip}"
            logger.info(f"Connecting with: {ssh_command}")
            os.system(ssh_command)
            return

        # Execute the SSH command for non-interactive mode
        try:
            subprocess.run(ssh_command, check=True)
        except subprocess.CalledProcessError as e:
            logger.error(f"SSH connection failed: {e}")

    @staticmethod
    def stop(
        project_name: str = config.PROJECT_NAME,
        security_group_name: str = config.AWS_EC2_SECURITY_GROUP,
    ) -> None:
        """
        Terminates the EC2 instance and deletes the associated security group.

        Args:
            project_name (str): The project name used to tag the instance. Defaults to config.PROJECT_NAME.
            security_group_name (str): The name of the security group to delete. Defaults to config.AWS_EC2_SECURITY_GROUP.
        """
        ec2_resource = boto3.resource('ec2')
        ec2_client = boto3.client('ec2')

        # Terminate EC2 instances
        instances = ec2_resource.instances.filter(
            Filters=[
                {'Name': 'tag:Name', 'Values': [project_name]},
                {'Name': 'instance-state-name', 'Values': ['pending', 'running', 'shutting-down', 'stopped', 'stopping']}
            ]
        )

        for instance in instances:
            logger.info(f"Terminating instance: ID - {instance.id}")
            instance.terminate()
            instance.wait_until_terminated()
            logger.info(f"Instance {instance.id} terminated successfully.")

        # Delete security group
        try:
            ec2_client.delete_security_group(GroupName=security_group_name)
            logger.info(f"Deleted security group: {security_group_name}")
        except ClientError as e:
            if e.response['Error']['Code'] == 'InvalidGroup.NotFound':
                logger.info(f"Security group {security_group_name} does not exist or already deleted.")
            else:
                logger.error(f"Error deleting security group: {e}")


if __name__ == "__main__":
    fire.Fire(Deploy)
