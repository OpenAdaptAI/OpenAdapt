#!/usr/bin/env python3
"""GPU instance lifecycle CLI: launch EC2 GPU, serve model, SSH tunnel, terminate.

State at ~/.openadapt/gpu_state.json so terminate always works (even if SSH is broken).
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import signal
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("gpu_cli")

STATE_FILE = Path.home() / ".openadapt" / "gpu_state.json"
DEFAULTS = dict(
    instance_type="g5.xlarge", region="us-east-1", key_name="openadapt-grpo",
    engine="sglang", port=8080, model="Qwen/Qwen3.5-9B",
)
SSH_OPTS = [
    "-o", "StrictHostKeyChecking=no", "-o", "UserKnownHostsFile=/dev/null",
    "-o", "LogLevel=ERROR", "-o", "ConnectTimeout=10",
    "-o", "ServerAliveInterval=60", "-o", "ServerAliveCountMax=10",
]
SSH_USER = "ubuntu"
_DL_AMI_NAME = "Deep Learning OSS Nvidia Driver AMI GPU PyTorch *Ubuntu 22.04*"
_DL_AMI_OWNER = "898082745236"


def _save_state(s: dict[str, Any]) -> None:
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    s["updated_at"] = datetime.now(timezone.utc).isoformat()
    STATE_FILE.write_text(json.dumps(s, indent=2))


def _load_state() -> dict[str, Any] | None:
    if not STATE_FILE.exists():
        return None
    try:
        return json.loads(STATE_FILE.read_text())
    except (json.JSONDecodeError, OSError):
        return None


def _clear_state() -> None:
    if STATE_FILE.exists():
        STATE_FILE.unlink()


def _ssh(ip: str, cmd: str, stream: bool = False, timeout: int = 600):
    full = ["ssh", *SSH_OPTS, f"{SSH_USER}@{ip}", cmd]
    if stream:
        proc = subprocess.Popen(
            full, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1
        )
        try:
            for line in iter(proc.stdout.readline, ""):
                if line.strip():
                    logger.info("[remote] %s", line.rstrip())
            proc.wait()
        except KeyboardInterrupt:
            proc.terminate()
            return subprocess.CompletedProcess(cmd, 130)
        return subprocess.CompletedProcess(cmd, proc.returncode)
    return subprocess.run(full, capture_output=True, text=True, timeout=timeout)


def _wait_for_ssh(ip: str, timeout: int = 300) -> bool:
    logger.info("Waiting for SSH on %s...", ip)
    t0 = time.time()
    while time.time() - t0 < timeout:
        try:
            r = subprocess.run(
                ["ssh", *SSH_OPTS, f"{SSH_USER}@{ip}", "echo ok"],
                capture_output=True, text=True, timeout=15,
            )
            if r.returncode == 0:
                logger.info("SSH ready")
                return True
        except subprocess.TimeoutExpired:
            pass
        time.sleep(5)
    return False


def _find_dl_ami(region: str) -> str:
    import boto3
    ec2 = boto3.client("ec2", region_name=region)
    resp = ec2.describe_images(Owners=[_DL_AMI_OWNER], Filters=[
        {"Name": "name", "Values": [_DL_AMI_NAME]},
        {"Name": "architecture", "Values": ["x86_64"]},
        {"Name": "state", "Values": ["available"]},
    ])
    images = sorted(resp["Images"], key=lambda x: x["CreationDate"], reverse=True)
    if not images:
        raise RuntimeError(f"No Deep Learning AMI found in {region}")
    logger.info("AMI: %s (%s)", images[0]["ImageId"], images[0]["Name"])
    return images[0]["ImageId"]


def _launch_instance(ami: str, instance_type: str, region: str, key_name: str) -> str:
    import boto3
    ec2c = boto3.client("ec2", region_name=region)
    ec2r = boto3.resource("ec2", region_name=region)
    sg_id = subnet_id = None
    try:
        sgs = ec2c.describe_security_groups(
            Filters=[{"Name": "group-name", "Values": ["waa-pool-infra"]}]
        )["SecurityGroups"]
        if sgs:
            sg_id = sgs[0]["GroupId"]
    except Exception:
        pass
    try:
        subs = ec2c.describe_subnets(
            Filters=[{"Name": "tag:Name", "Values": ["waa-pool-infra"]}]
        )["Subnets"]
        if subs:
            subnet_id = subs[0]["SubnetId"]
    except Exception:
        pass
    try:
        ec2c.describe_key_pairs(KeyNames=[key_name])
    except Exception as e:
        if "InvalidKeyPair.NotFound" in str(e):
            pub = Path.home() / ".ssh" / "id_rsa.pub"
            if not pub.exists():
                raise RuntimeError(f"No SSH key at {pub}. Run: ssh-keygen -t rsa -b 4096")
            ec2c.import_key_pair(KeyName=key_name, PublicKeyMaterial=pub.read_bytes())
        else:
            raise
    kw: dict[str, Any] = dict(
        ImageId=ami, InstanceType=instance_type, KeyName=key_name, MinCount=1, MaxCount=1,
        BlockDeviceMappings=[{"DeviceName": "/dev/sda1",
            "Ebs": {"VolumeSize": 200, "VolumeType": "gp3", "DeleteOnTermination": True}}],
        TagSpecifications=[{"ResourceType": "instance",
            "Tags": [{"Key": "Name", "Value": "openadapt-gpu"},
                      {"Key": "openadapt-gpu", "Value": "true"}]}],
    )
    if subnet_id and sg_id:
        kw["NetworkInterfaces"] = [{"DeviceIndex": 0, "SubnetId": subnet_id,
            "Groups": [sg_id], "AssociatePublicIpAddress": True}]
    elif sg_id:
        kw["SecurityGroupIds"] = [sg_id]
    logger.info("Launching %s in %s...", instance_type, region)
    return ec2r.create_instances(**kw)[0].id


def _wait_for_instance(iid: str, region: str) -> str:
    import boto3
    ec2 = boto3.client("ec2", region_name=region)
    ec2.get_waiter("instance_running").wait(InstanceIds=[iid])
    ip = ec2.describe_instances(InstanceIds=[iid])["Reservations"][0]["Instances"][0].get(
        "PublicIpAddress"
    )
    if not ip:
        eip = ec2.allocate_address(Domain="vpc")
        ec2.associate_address(InstanceId=iid, AllocationId=eip["AllocationId"])
        ip = eip["PublicIp"]
    logger.info("Running at %s", ip)
    return ip


def _terminate_instance(iid: str, region: str) -> bool:
    """Terminate EC2 instance via API (works even if SSH is broken)."""
    try:
        import boto3
        ec2 = boto3.client("ec2", region_name=region)
        try:
            for a in ec2.describe_addresses(
                Filters=[{"Name": "instance-id", "Values": [iid]}]
            ).get("Addresses", []):
                if a.get("AssociationId"):
                    ec2.disassociate_address(AssociationId=a["AssociationId"])
                ec2.release_address(AllocationId=a["AllocationId"])
        except Exception:
            pass
        ec2.terminate_instances(InstanceIds=[iid])
        logger.info("Terminating %s...", iid)
        ec2.get_waiter("instance_terminated").wait(
            InstanceIds=[iid], WaiterConfig={"Delay": 10, "MaxAttempts": 30}
        )
        logger.info("Terminated")
        return True
    except Exception as e:
        logger.error("Terminate failed: %s", e)
        return False


def _install_engine(ip: str, engine: str) -> None:
    cmds = {
        "sglang": "pip install 'sglang[all]' --find-links https://flashinfer.ai/whl/cu124/torch2.5/flashinfer/",
        "vllm": "pip install vllm",
    }
    if engine not in cmds:
        raise ValueError(f"Unknown engine: {engine}")
    logger.info("Installing %s...", engine)
    if _ssh(ip, cmds[engine], stream=True, timeout=600).returncode != 0:
        raise RuntimeError(f"Failed to install {engine}")


def _serve_model(ip: str, model: str, engine: str, port: int) -> None:
    logger.info("Starting %s for %s on :%d...", engine, model, port)
    _ssh(ip, f"fuser -k {port}/tcp 2>/dev/null || true")
    time.sleep(2)
    if engine == "sglang":
        cmd = (f"nohup python3 -m sglang.launch_server --model-path {model} "
               f"--port {port} --host 0.0.0.0 > /tmp/sglang_server.log 2>&1 &")
    else:
        cmd = (f"nohup python3 -m vllm.entrypoints.openai.api_server --model {model} "
               f"--port {port} --host 0.0.0.0 > /tmp/vllm_server.log 2>&1 &")
    _ssh(ip, cmd)


def _wait_for_model(ip: str, port: int, timeout: int = 600) -> bool:
    logger.info("Waiting for model on :%d...", port)
    t0 = time.time()
    health = f"curl -sf http://localhost:{port}/health || curl -sf http://localhost:{port}/v1/models"
    while time.time() - t0 < timeout:
        if _ssh(ip, health).returncode == 0:
            logger.info("Model ready (%.0fs)", time.time() - t0)
            return True
        if _ssh(ip, f"fuser {port}/tcp 2>/dev/null").returncode != 0:
            logger.error("Server died. Logs:\n%s",
                         _ssh(ip, "tail -20 /tmp/*_server.log 2>/dev/null").stdout)
            return False
        elapsed = int(time.time() - t0)
        if elapsed > 0 and elapsed % 30 == 0:
            logger.info("Loading... (%ds)", elapsed)
        time.sleep(10)
    return False


def _setup_tunnel(ip: str, port: int) -> None:
    _kill_tunnel(port)
    r = subprocess.run(
        ["ssh", "-f", "-N", *SSH_OPTS, "-L", f"{port}:localhost:{port}", f"{SSH_USER}@{ip}"],
        capture_output=True, text=True,
    )
    if r.returncode != 0:
        raise RuntimeError(f"Tunnel failed: {r.stderr}")
    logger.info("Tunnel: localhost:%d -> %s:%d", port, ip, port)


def _kill_tunnel(port: int) -> None:
    try:
        r = subprocess.run(
            ["lsof", "-ti", f":{port}", "-sTCP:LISTEN"], capture_output=True, text=True
        )
        for pid in (r.stdout.strip().split("\n") if r.stdout.strip() else []):
            os.kill(int(pid.strip()), signal.SIGTERM)
    except Exception:
        pass


# -- CLI commands ----------------------------------------------------------

def cmd_launch(args) -> int:
    existing = _load_state()
    if existing and not getattr(args, "force", False):
        logger.error("Instance tracked: %s. 'terminate' first or --force.", existing.get("instance_id"))
        return 1
    if existing and getattr(args, "force", False):
        _terminate_instance(existing["instance_id"], existing.get("region", DEFAULTS["region"]))
        _clear_state()
    try:
        ami = _find_dl_ami(args.region)
        iid = _launch_instance(ami, args.instance_type, args.region, args.key_name)
        state = {"instance_id": iid, "region": args.region, "instance_type": args.instance_type,
                 "created_at": datetime.now(timezone.utc).isoformat()}
        _save_state(state)
        ip = _wait_for_instance(iid, args.region)
        state["ip"] = ip
        _save_state(state)
        if not _wait_for_ssh(ip):
            logger.error("SSH timeout. Try: ssh %s@%s", SSH_USER, ip)
            return 1
        _install_engine(ip, args.engine)
        _serve_model(ip, args.model, args.engine, args.port)
        if not _wait_for_model(ip, args.port):
            logger.error("Model failed. Check: ssh %s@%s 'cat /tmp/%s_server.log'",
                         SSH_USER, ip, args.engine)
            return 1
        _setup_tunnel(ip, args.port)
        state.update(model=args.model, engine=args.engine, port=args.port, status="ready")
        _save_state(state)
        print(f"\nReady! {iid} IP={ip} Model={args.model} http://localhost:{args.port}/v1")
        print("To terminate: openadapt-gpu terminate")
        return 0
    except Exception as e:
        logger.error("Launch failed: %s", e)
        s = _load_state()
        if s and s.get("instance_id"):
            _terminate_instance(s["instance_id"], args.region)
            _clear_state()
        return 1


def cmd_status(_) -> int:
    state = _load_state()
    if not state:
        print("No GPU instance tracked.")
        return 0
    for k in ["instance_id", "region", "instance_type", "ip", "model", "engine",
              "port", "status", "created_at"]:
        print(f"  {k:15s} {state.get(k, '-')}")
    try:
        import boto3
        ec2 = boto3.client("ec2", region_name=state.get("region", DEFAULTS["region"]))
        r = ec2.describe_instances(InstanceIds=[state["instance_id"]])
        print(f"  {'aws_state':15s} {r['Reservations'][0]['Instances'][0]['State']['Name']}")
    except Exception as e:
        print(f"  {'aws_state':15s} error ({e})")
    port = state.get("port")
    if port:
        r = subprocess.run(["lsof", "-ti", f":{port}", "-sTCP:LISTEN"],
                           capture_output=True, text=True)
        print(f"  {'tunnel':15s} {'alive' if r.stdout.strip() else 'dead'}")
    return 0


def cmd_terminate(_) -> int:
    state = _load_state()
    if not state:
        print("No GPU instance tracked.")
        return 0
    if state.get("port"):
        _kill_tunnel(state["port"])
    if state.get("instance_id"):
        ok = _terminate_instance(state["instance_id"], state.get("region", DEFAULTS["region"]))
        if not ok:
            logger.warning("Verify: aws ec2 describe-instances --instance-ids %s --region %s",
                           state["instance_id"], state.get("region"))
    _clear_state()
    print("GPU instance terminated.")
    return 0


def cmd_serve(args) -> int:
    state = _load_state()
    if not state or not state.get("ip"):
        logger.error("No running instance. Use 'launch' first.")
        return 1
    ip = state["ip"]
    engine = args.engine or state.get("engine", DEFAULTS["engine"])
    port = args.port or state.get("port", DEFAULTS["port"])
    if not _wait_for_ssh(ip, timeout=30):
        logger.error("SSH unreachable at %s", ip)
        return 1
    _serve_model(ip, args.model, engine, port)
    if not _wait_for_model(ip, port):
        return 1
    _setup_tunnel(ip, port)
    state.update(model=args.model, engine=engine, port=port, status="ready")
    _save_state(state)
    print(f"Serving {args.model} at http://localhost:{port}/v1")
    return 0


def cmd_run_comparison(args) -> int:
    cfg = Path(args.config)
    if not cfg.exists():
        logger.error("Config not found: %s", cfg)
        return 1
    la = argparse.Namespace(
        model=args.model or DEFAULTS["model"], engine=args.engine or DEFAULTS["engine"],
        port=args.port or DEFAULTS["port"], instance_type=args.instance_type,
        region=args.region, key_name=args.key_name, force=False,
    )
    try:
        if cmd_launch(la) != 0:
            return 1
        cmd = [sys.executable, "-m", "scripts.compare_models", "--config", str(cfg)]
        if args.server_url:
            cmd += ["--server-url", args.server_url]
        if args.resume:
            cmd.append("--resume")
        if args.output:
            cmd += ["--output", args.output]
        return subprocess.run(cmd, cwd=str(Path(__file__).resolve().parent.parent)).returncode
    finally:
        cmd_terminate(argparse.Namespace())


def cmd_ssh(args) -> int:
    state = _load_state()
    if not state or not state.get("ip"):
        logger.error("No running instance.")
        return 1
    cmd = ["ssh", *SSH_OPTS, f"{SSH_USER}@{state['ip']}"]
    if args.command:
        cmd.append(args.command)
    os.execvp("ssh", cmd)


def cmd_logs(args) -> int:
    state = _load_state()
    if not state or not state.get("ip"):
        logger.error("No running instance.")
        return 1
    engine = state.get("engine", DEFAULTS["engine"])
    r = _ssh(state["ip"], f"tail -{args.lines} /tmp/{engine}_server.log 2>/dev/null")
    print(r.stdout if r.returncode == 0 else f"No logs. Try: ssh {SSH_USER}@{state['ip']}")
    return 0


# -- Argument parser -------------------------------------------------------

def _add_gpu_args(p, include_model=True):
    if include_model:
        p.add_argument("--model", default=DEFAULTS["model"])
    p.add_argument("--engine", default=DEFAULTS["engine"], choices=["sglang", "vllm"])
    p.add_argument("--port", type=int, default=DEFAULTS["port"])
    p.add_argument("--instance-type", default=DEFAULTS["instance_type"])
    p.add_argument("--region", default=DEFAULTS["region"])
    p.add_argument("--key-name", default=DEFAULTS["key_name"])


def main():
    parser = argparse.ArgumentParser(prog="openadapt-gpu", description="GPU instance lifecycle CLI")
    sub = parser.add_subparsers(dest="command")

    p = sub.add_parser("launch", help="Launch GPU, install engine, serve model, tunnel")
    _add_gpu_args(p)
    p.add_argument("--force", action="store_true", help="Terminate existing first")

    sub.add_parser("status", help="Show instance status")
    sub.add_parser("terminate", help="Terminate instance (stops billing)")

    p = sub.add_parser("serve", help="Serve different model on running instance")
    p.add_argument("--model", required=True)
    p.add_argument("--engine", choices=["sglang", "vllm"])
    p.add_argument("--port", type=int)

    p = sub.add_parser("run-comparison", help="Launch + compare + terminate")
    p.add_argument("--config", required=True)
    _add_gpu_args(p)
    p.add_argument("--server-url")
    p.add_argument("--resume", action="store_true")
    p.add_argument("--output")

    p = sub.add_parser("ssh", help="SSH into GPU instance")
    p.add_argument("command", nargs="?")

    p = sub.add_parser("logs", help="Show server logs")
    p.add_argument("--lines", "-n", type=int, default=50)

    args = parser.parse_args()
    if not args.command:
        parser.print_help()
        sys.exit(1)
    dispatch = {
        "launch": cmd_launch, "status": cmd_status, "terminate": cmd_terminate,
        "serve": cmd_serve, "run-comparison": cmd_run_comparison,
        "ssh": cmd_ssh, "logs": cmd_logs,
    }
    sys.exit(dispatch[args.command](args))


if __name__ == "__main__":
    main()
