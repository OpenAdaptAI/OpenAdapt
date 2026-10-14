"""End-to-end verl-agent training orchestration.

Provisions a GPU VM, installs verl-agent, connects to a WAA server,
and launches GiGPO/GRPO training. Reuses existing openadapt-evals
infrastructure (AzureVMManager, AWSVMManager, SSHTunnelManager, ssh_run).

Architecture:
    LOCAL MACHINE          GPU VM (training)         CPU VM (WAA)
    +--------------+       +------------------+      +----------------+
    | This script  | SSH   | verl-agent       |      | Docker         |
    | (orchestrate)| ----> | WAADesktopEnv    | HTTP | QEMU (Win 11)  |
    |              |       | vLLM + Ray       | ---> | WAA Flask API  |
    +--------------+       +------------------+      +----------------+

The GPU VM runs verl-agent training. WAADesktopEnv connects to the WAA
server on the CPU VM via HTTP. The CPU VM can be an existing pool worker
or created fresh.

Usage:
    # Azure (provisions GPU VM + connects to existing WAA server)
    python scripts/train_verl_e2e.py \\
        --cloud azure \\
        --waa-server http://localhost:5000 \\
        --evaluate-server http://localhost:5001 \\
        --task-id <WAA_UUID>

    # AWS (provisions both GPU VM and WAA VM)
    python scripts/train_verl_e2e.py \\
        --cloud aws \\
        --task-id <WAA_UUID>

    # Dry run (show what would happen)
    python scripts/train_verl_e2e.py --dry-run --cloud azure

    # Use existing GPU VM
    python scripts/train_verl_e2e.py \\
        --gpu-ip 52.170.1.100 \\
        --waa-server http://localhost:5000 \\
        --evaluate-server http://localhost:5001 \\
        --task-id <WAA_UUID>
"""

from __future__ import annotations

import argparse
import logging
import subprocess
import sys
import time
from pathlib import Path

logger = logging.getLogger(__name__)

SETUP_SCRIPT = Path(__file__).parent / "setup_gpu_training.sh"
GPU_VM_NAME = "verl-train-00"
AUTO_SHUTDOWN_HOURS = 6


def _get_vm_manager(cloud: str):
    """Get the appropriate VM manager for the cloud provider."""
    if cloud == "azure":
        from openadapt_evals.infrastructure.azure_vm import AzureVMManager

        return AzureVMManager()
    elif cloud == "aws":
        from openadapt_evals.infrastructure.aws_vm import AWSVMManager

        return AWSVMManager()
    else:
        raise ValueError(f"Unknown cloud: {cloud}")


def _ssh_run(ip: str, cmd: str, username: str = "ubuntu", stream: bool = True):
    """Run a command on the remote VM via SSH with streaming output."""
    from openadapt_evals.infrastructure.azure_vm import ssh_run

    return ssh_run(ip=ip, cmd=cmd, stream=stream, username=username)


def _scp_upload(ip: str, local_path: Path, remote_path: str, username: str = "ubuntu"):
    """Upload a file to the remote VM via SCP."""
    from openadapt_evals.infrastructure.azure_vm import SSH_OPTS

    cmd = ["scp", *SSH_OPTS, str(local_path), f"{username}@{ip}:{remote_path}"]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(f"SCP failed: {result.stderr}")


def provision_gpu_vm(
    cloud: str, dry_run: bool = False, spot: bool = False,
) -> tuple[str, str, str]:
    """Provision a GPU VM and return (ip, size, region).

    Args:
        cloud: Cloud provider ("azure" or "aws").
        dry_run: If True, show what would happen without creating anything.
        spot: If True, request a spot instance for cost savings (AWS only).

    Returns:
        Tuple of (public_ip, vm_size, region).
    """
    vm = _get_vm_manager(cloud)
    username = vm.ssh_username

    logger.info("Finding available GPU VM size...")
    vm_size, region, cost = vm.find_available_size_and_region(gpu=True)
    spot_label = " (spot)" if spot else ""
    logger.info("Selected: %s ($%.2f/hr)%s in %s", vm_size, cost, spot_label, region)

    if dry_run:
        logger.info("[DRY RUN] Would create %s%s in %s", GPU_VM_NAME, spot_label, region)
        return ("DRY_RUN_IP", vm_size, region)

    logger.info("Creating GPU VM '%s'%s...", GPU_VM_NAME, spot_label)
    create_kwargs: dict = dict(name=GPU_VM_NAME, region=region, size=vm_size, gpu=True)
    if spot and cloud == "aws":
        create_kwargs["spot"] = True
    elif spot and cloud != "aws":
        logger.warning("--spot is only supported on AWS; ignoring for %s", cloud)
    info = vm.create_vm(**create_kwargs)
    ip = info.get("publicIpAddress") or vm.get_vm_ip(GPU_VM_NAME)

    if not ip:
        raise RuntimeError(f"Failed to get IP for {GPU_VM_NAME}")

    logger.info("GPU VM created: %s (%s)", ip, vm_size)

    # Set auto-shutdown to prevent runaway costs
    vm.set_auto_shutdown(GPU_VM_NAME, hours=AUTO_SHUTDOWN_HOURS)
    logger.info("Auto-shutdown set to %d hours", AUTO_SHUTDOWN_HOURS)

    # Wait for SSH
    logger.info("Waiting for SSH...")
    for attempt in range(30):
        try:
            result = _ssh_run(ip, "echo ready", username=username, stream=False)
            if result.returncode == 0:
                break
        except Exception:
            pass
        time.sleep(10)
    else:
        raise RuntimeError(f"SSH not ready after 5 minutes: {ip}")

    return (ip, vm_size, region)


def setup_training(ip: str, username: str = "ubuntu"):
    """Install verl-agent and dependencies on the GPU VM."""
    logger.info("Uploading setup script...")
    _scp_upload(ip, SETUP_SCRIPT, "/tmp/setup_gpu_training.sh", username=username)

    logger.info("Running setup (this may take 15-30 minutes)...")
    result = _ssh_run(
        ip,
        "bash /tmp/setup_gpu_training.sh",
        username=username,
        stream=True,
    )
    if result.returncode != 0:
        raise RuntimeError(f"Setup failed with exit code {result.returncode}")

    logger.info("Setup complete!")


def prepare_training_data(ip: str, group_size: int = 8, username: str = "ubuntu"):
    """Prepare parquet data files required by verl-agent.

    verl-agent requires train/val parquet files even for env-based training.
    These define the modality (text vs visual) and batch sizing.

    After running VAGEN's data preprocessor, validates that the expected
    output files exist and are non-empty.
    """
    logger.info("Preparing training data (parquet files)...")
    prep_cmd = (
        "cd ~/verl-agent && "
        "conda run -n verl-agent python3 -m examples.data_preprocess.prepare "
        f"--mode visual --train_data_size {group_size} --val_data_size 128"
    )
    result = _ssh_run(ip, prep_cmd, username=username, stream=True)
    if result.returncode != 0:
        raise RuntimeError("Data preparation failed")

    # Validate output files exist and are non-empty
    validate_cmd = (
        'for f in ~/data/verl-agent/visual/train.parquet ~/data/verl-agent/visual/test.parquet; do '
        '  if [ ! -s "$f" ]; then echo "MISSING_OR_EMPTY: $f"; exit 1; fi; '
        '  echo "OK: $f ($(stat --format=%s "$f" 2>/dev/null || stat -f%z "$f") bytes)"; '
        'done'
    )
    result = _ssh_run(ip, validate_cmd, username=username, stream=True)
    if result.returncode != 0:
        raise RuntimeError(
            "Training data validation failed: expected train.parquet and "
            "test.parquet in ~/data/verl-agent/visual/"
        )


def register_waa_env(ip: str, username: str = "ubuntu"):
    """Register WAADesktopEnv in VAGEN's env registry on the GPU VM.

    VAGEN uses a YAML registry (vagen/configs/env_registry.yaml) to dispatch
    environments by name. We add a 'WAADesktop' entry pointing to our
    WAADesktopEnv class. This is the VAGEN-native approach — no monkey-patching.
    """
    logger.info("Registering WAADesktopEnv in VAGEN env registry...")

    register_script = '''
import os, sys, yaml

# Find env_registry.yaml in VAGEN installation
candidates = [
    os.path.expanduser("~/verl-agent/vagen/configs/env_registry.yaml"),
    os.path.expanduser("~/VAGEN/vagen/configs/env_registry.yaml"),
]

registry_path = None
for c in candidates:
    if os.path.exists(c):
        registry_path = c
        break

if registry_path is None:
    # Try finding it via the vagen package
    try:
        import vagen
        pkg_dir = os.path.dirname(vagen.__file__)
        registry_path = os.path.join(pkg_dir, "configs", "env_registry.yaml")
    except ImportError:
        pass

if registry_path is None or not os.path.exists(registry_path):
    print("ERROR: Cannot find vagen/configs/env_registry.yaml")
    print("Searched:", candidates)
    sys.exit(1)

with open(registry_path, "r") as f:
    config = yaml.safe_load(f) or {}

registry = config.get("env_registry", {})

if "WAADesktop" in registry:
    print(f"WAADesktop already registered in {registry_path}")
    sys.exit(0)

registry["WAADesktop"] = "openadapt_evals.adapters.verl_env.WAADesktopEnv"
config["env_registry"] = registry

with open(registry_path, "w") as f:
    yaml.dump(config, f, default_flow_style=False)

print(f"Registered WAADesktop in {registry_path}")
print(f"Registry now contains: {list(registry.keys())}")
'''

    # Write script to a temp file to avoid nested quoting issues
    _ssh_run(
        ip,
        f"cat > /tmp/_register_env.py << 'PYEOF'\n{register_script}\nPYEOF",
        username=username,
        stream=False,
    )
    result = _ssh_run(
        ip,
        "conda run -n verl-agent python3 /tmp/_register_env.py",
        username=username,
        stream=True,
    )
    if result.returncode != 0:
        logger.warning("Registry update failed; trying programmatic registration...")
        # Fallback: use our register_in_vagen() helper
        fallback_cmd = (
            "conda run -n verl-agent python3 -c "
            '"from openadapt_evals.adapters.verl_env import register_in_vagen; '
            "register_in_vagen() or print("
            "'WARNING: Could not register WAADesktopEnv')\""
        )
        _ssh_run(ip, fallback_cmd, username=username, stream=True)


def _generate_training_config(
    ip: str,
    waa_server: str,
    task_id: str,
    algorithm: str,
    model: str,
    n_gpus: int,
    max_turns: int,
    group_size: int,
    epochs: int,
    username: str,
    evaluate_url: str | None = None,
) -> str:
    """Generate a VAGEN training config YAML on the GPU VM.

    Returns the remote path to the generated config file.
    """
    import json

    config = {
        "envs": [
            {
                "name": "WAADesktop",
                "n_envs": 1,
                "data_source": "waa",
                "seed": [1, 100, 1],
                "max_turns": max_turns,
                "response_length_per_turn": 512,
                "config": {
                    "server_url": waa_server,
                    **({"evaluate_url": evaluate_url} if evaluate_url else {}),
                    "task_id": task_id,
                    "max_steps": max_turns,
                    "evaluate_at_done": True,
                    "action_type": "fractional",
                },
            }
        ],
    }

    # Upload config as YAML
    config_script = f"""
import yaml, os
config = {json.dumps(config)}
path = os.path.expanduser("~/waa_training_config.yaml")
with open(path, "w") as f:
    yaml.dump(config, f, default_flow_style=False)
print(f"Training config written to {{path}}")
"""
    _ssh_run(
        ip,
        f"conda run -n verl-agent python3 -c '{config_script}'",
        username=username,
        stream=True,
    )
    return "~/waa_training_config.yaml"


def launch_training(
    ip: str,
    waa_server: str,
    task_id: str,
    algorithm: str = "gigpo",
    model: str = "Qwen/Qwen2.5-VL-3B-Instruct",
    n_gpus: int = 2,
    max_turns: int = 15,
    group_size: int = 8,
    epochs: int = 100,
    username: str = "ubuntu",
    evaluate_url: str | None = None,
):
    """Launch VAGEN training on the GPU VM.

    VAGEN uses GymImageEnv environments with a YAML registry. Our
    WAADesktopEnv is registered as 'WAADesktop' and connects to the
    WAA server via HTTP for environment interaction.

    The training flow:
    1. Register WAADesktopEnv in VAGEN's env registry
    2. Prepare training data (parquet files for VAGEN's dataset loader)
    3. Generate training config YAML
    4. Launch training via VAGEN's entry point
    """
    # Step 1: Register WAADesktopEnv in VAGEN's env registry
    register_waa_env(ip, username=username)

    # Step 2: Prepare parquet data files (required by VAGEN's AgenticDataset)
    prepare_training_data(ip, group_size=group_size, username=username)

    # Step 3: Generate training config
    config_path = _generate_training_config(
        ip=ip,
        waa_server=waa_server,
        task_id=task_id,
        algorithm=algorithm,
        model=model,
        n_gpus=n_gpus,
        max_turns=max_turns,
        group_size=group_size,
        epochs=epochs,
        username=username,
        evaluate_url=evaluate_url,
    )

    # Step 4: Launch training
    # VAGEN uses its own entry point (vagen.main_ppo) with Hydra config.
    # The env spec YAML provides the environment definition;
    # Hydra overrides configure the verl training loop.
    train_cmd = f"""
cd ~/verl-agent && \\
conda run -n verl-agent python3 -m vagen.main_ppo \\
    --config-path=$HOME/verl-agent/vagen/configs \\
    --config-name=vagen_multiturn \\
    data.train_files={config_path} \\
    data.val_files={config_path} \\
    data.train_batch_size=1 \\
    data.max_prompt_length=2048 \\
    data.max_response_length=512 \\
    data.return_raw_chat=True \\
    data.return_multi_modal_inputs=True \\
    algorithm.adv_estimator={algorithm} \\
    algorithm.kl_ctrl.kl_coef=0.0 \\
    algorithm.gamma={'0.95' if algorithm == 'gigpo' else '1.0'} \\
    actor_rollout_ref.model.path={model} \\
    actor_rollout_ref.model.enable_gradient_checkpointing=True \\
    actor_rollout_ref.actor.optim.lr=1e-6 \\
    actor_rollout_ref.actor.ppo_mini_batch_size=1 \\
    actor_rollout_ref.actor.ppo_micro_batch_size_per_gpu=1 \\
    actor_rollout_ref.actor.fsdp_config.param_offload=True \\
    actor_rollout_ref.actor.fsdp_config.optimizer_offload=True \\
    actor_rollout_ref.rollout.name=vllm \\
    actor_rollout_ref.rollout.mode=async \\
    actor_rollout_ref.rollout.n={group_size} \\
    actor_rollout_ref.rollout.tensor_model_parallel_size=1 \\
    actor_rollout_ref.rollout.gpu_memory_utilization=0.5 \\
    actor_rollout_ref.rollout.enforce_eager=True \\
    actor_rollout_ref.rollout.enable_chunked_prefill=True \\
    actor_rollout_ref.rollout.multi_turn.enable=True \\
    actor_rollout_ref.rollout.agent.agent_loop_config_path=$HOME/verl-agent/vagen/configs/agent.yaml \\
    actor_rollout_ref.ref.fsdp_config.param_offload=True \\
    trainer.n_gpus_per_node={n_gpus} \\
    trainer.nnodes=1 \\
    trainer.total_training_steps={epochs} \\
    trainer.test_freq=5 \\
    trainer.save_freq=25 \\
    trainer.val_before_train=True \\
    trainer.logger=['console','wandb'] \\
    trainer.project_name=openadapt-waa-rl \\
    trainer.experiment_name={algorithm}_waa_desktop
"""
    logger.info("Launching training with %s on %d GPU(s)...", algorithm, n_gpus)
    logger.info("Model: %s", model)
    logger.info("WAA server: %s", waa_server)
    logger.info("Evaluate server: %s", evaluate_url)
    logger.info("Task: %s", task_id)

    result = _ssh_run(ip, train_cmd, username=username, stream=True)
    return result.returncode


def cleanup_gpu_vm(cloud: str):
    """Deallocate (not delete) the GPU VM to stop billing."""
    vm = _get_vm_manager(cloud)
    logger.info("Deallocating GPU VM '%s'...", GPU_VM_NAME)
    vm.deallocate_vm(GPU_VM_NAME)
    logger.info("GPU VM deallocated (disk preserved, billing stopped)")


def main():
    parser = argparse.ArgumentParser(
        description="End-to-end verl-agent training on a GPU VM",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--cloud", choices=["azure", "aws"], default="azure",
        help="Cloud provider (default: azure)",
    )
    parser.add_argument(
        "--gpu-ip", type=str, default=None,
        help="Use an existing GPU VM instead of provisioning one",
    )
    parser.add_argument(
        "--waa-server", type=str, default="http://localhost:5000",
        help="WAA Flask API URL (screenshots, actions) (default: http://localhost:5000)",
    )
    parser.add_argument(
        "--evaluate-server", type=str, default="http://localhost:5001",
        help="Evaluate server URL (setup, evaluate) (default: http://localhost:5001)",
    )
    parser.add_argument(
        "--task-id", type=str, required=True,
        help="WAA task UUID to train on",
    )
    parser.add_argument(
        "--algorithm", choices=["gigpo", "grpo", "ppo"], default="gigpo",
        help="RL algorithm (default: gigpo)",
    )
    parser.add_argument(
        "--model", type=str, default="Qwen/Qwen2.5-VL-3B-Instruct",
        help="Model to train (default: Qwen/Qwen2.5-VL-3B-Instruct)",
    )
    parser.add_argument(
        "--n-gpus", type=int, default=1,
        help="Number of GPUs per node (default: 1, use 4 for g5.12xlarge)",
    )
    parser.add_argument(
        "--epochs", type=int, default=100,
        help="Training epochs (default: 100)",
    )
    parser.add_argument(
        "--spot", action="store_true",
        help="Use spot instances for cost savings (AWS only, ~50%% cheaper)",
    )
    parser.add_argument(
        "--setup-only", action="store_true",
        help="Only provision and setup, don't start training",
    )
    parser.add_argument(
        "--skip-setup", action="store_true",
        help="Skip setup (VM already configured)",
    )
    parser.add_argument(
        "--cleanup", action="store_true",
        help="Deallocate the GPU VM after training",
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Show what would happen without doing it",
    )
    parser.add_argument(
        "-v", "--verbose", action="store_true",
        help="Verbose logging",
    )
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
    )

    username = "azureuser" if args.cloud == "azure" else "ubuntu"

    try:
        # Step 1: Provision GPU VM
        if args.gpu_ip:
            ip = args.gpu_ip
            logger.info("Using existing GPU VM: %s", ip)
        else:
            ip, vm_size, region = provision_gpu_vm(
                args.cloud, dry_run=args.dry_run, spot=args.spot,
            )
            if args.dry_run:
                logger.info("[DRY RUN] Would setup and train on %s", vm_size)
                return

        # Step 2: Install dependencies
        if not args.skip_setup:
            setup_training(ip, username=username)

        if args.setup_only:
            logger.info("Setup complete. VM ready at: %s", ip)
            logger.info("SSH: ssh %s@%s", username, ip)
            return

        # Step 3: Launch training
        exit_code = launch_training(
            ip=ip,
            waa_server=args.waa_server,
            task_id=args.task_id,
            algorithm=args.algorithm,
            model=args.model,
            n_gpus=args.n_gpus,
            epochs=args.epochs,
            username=username,
            evaluate_url=args.evaluate_server,
        )

        if exit_code != 0:
            logger.error("Training exited with code %d", exit_code)
            sys.exit(exit_code)

        logger.info("Training complete!")

    finally:
        if args.cleanup and not args.dry_run and not args.gpu_ip:
            cleanup_gpu_vm(args.cloud)


if __name__ == "__main__":
    main()
