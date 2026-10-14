# VM Pool Optimization: Fast Startup with Cost Controls

## Problem

Creating a WAA evaluation pool currently takes **~42 minutes** end-to-end:

| Phase | Time | % |
|-------|------|---|
| Find VM size/region (test VM probe) | 2-5 min | 7% |
| Create VM (Azure API) | 5 min | 12% |
| Wait for SSH | 1-2 min | 4% |
| Docker install + image pull + build | 5-10 min | 19% |
| Windows boot + OOBE + WAA init | 15-25 min | 55% |
| SSH tunnel setup | <1 min | 2% |

For iterative development (run eval → analyze → tweak → re-run), this 42-minute cold start is the primary bottleneck.

## Goal

Reduce pool startup time to:
- **~5 min** on warm start (deallocated VM with persisted state)
- **~15 min** on cold start (new VM from pre-baked image)
- **$0 idle cost** when no eval is running (auto-teardown)

## Design: Four Optimizations

### 1. Pre-baked Azure Managed Image (cold start: 42 → 15 min)

**What**: Create a "golden image" of an Ubuntu VM with Docker installed, all container images pulled, and `waa-auto:latest` pre-built. New VMs boot from this image instead of bare Ubuntu.

**How it works**:
1. One-time: run full pool-create, wait for Docker setup to complete
2. Deallocate the VM, generalize it, create Azure Managed Image
3. Future pool-create uses `--image` flag to skip Docker setup entirely

**Implementation**:

```
oa-vm image-create    # One-time: create golden image from current VM
oa-vm image-list      # List available images
oa-vm image-delete    # Delete an image
```

New commands in `vm_cli.py`:

```python
def cmd_image_create(args):
    """Create golden image from existing pool VM."""
    # 1. Verify pool exists and Docker is set up
    # 2. Stop WAA container (clean state)
    # 3. Clean apt cache, docker build cache, /tmp
    # 4. az vm deallocate -n waa-pool-00
    # 5. az vm generalize -n waa-pool-00
    # 6. az image create --name waa-golden-{timestamp}
    #    --source waa-pool-00 --hyper-v-generation V2
    # 7. Tag image with metadata (date, docker images present)
    # 8. Delete the source VM (can't reuse after generalize)
```

Changes to `AzureVMManager.create_vm()`:
- Accept optional `image_id` parameter
- When `image_id` is provided, use it instead of `_UBUNTU_2204_IMAGE`
- SDK path: set `storage_profile.image_reference.id = image_id`
- CLI path: `--image <image_id>` instead of `--image Ubuntu2204`

Changes to `PoolManager.create()`:
- Accept optional `image` parameter
- When image is provided, skip Docker setup phase entirely
- Still run WAA container start (WAA_START_SCRIPT)

**Time saved**: ~15 min (Docker install + pull + build)

**Cost**:
- Managed Image: ~$0.008/GB/month for the OS disk snapshot
- For a 30GB disk: ~$0.24/month ≈ **$0.01/day**
- Negligible — no auto-teardown needed

**Risks**:
- Image becomes stale as Dockerfile changes → add `image-refresh` command
- Generalized VM can't be restarted → always create from the image
- Region-locked — image must be in same region as target VMs

---

### 2. Deallocate Instead of Delete (warm start: 42 → 5 min)

**What**: Stop billing for compute but keep the VM's disk, NIC, and IP. Next startup just re-allocates the VM (~2 min boot), and Docker container + Windows are already warm.

**How it works**:
```
# Instead of pool-cleanup (deletes everything):
oa-vm pool-pause       # Deallocate all VMs, stop compute billing
oa-vm pool-resume      # Start deallocated VMs, resume WAA

# Safety: auto-teardown after configurable idle period
```

**Cost model**:

| Resource | Running | Deallocated |
|----------|---------|-------------|
| VM compute (D8ds_v5) | $0.38/hr | $0.00 |
| OS Disk (64GB Premium SSD) | included | ~$0.15/day |
| Public IP (Static) | included | ~$0.10/day |
| NIC | $0.00 | $0.00 |
| **Total idle** | **$0.38/hr** | **~$0.25/day** |

**Implementation**:

New `AzureVMManager` methods:

```python
def deallocate_vm(self, name: str) -> bool:
    """Deallocate VM (stop billing, keep disk)."""
    # SDK: compute.virtual_machines.begin_deallocate(rg, name).result()
    # CLI: az vm deallocate -g rg -n name

def start_vm(self, name: str) -> bool:
    """Start a deallocated VM."""
    # SDK: compute.virtual_machines.begin_start(rg, name).result()
    # CLI: az vm start -g rg -n name
```

New `PoolManager` methods:

```python
def pause(self) -> bool:
    """Deallocate all pool VMs. Stops compute billing."""
    for worker in pool.workers:
        vm_manager.deallocate_vm(worker.name)
    registry.update_pool(status="paused")

def resume(self, timeout_minutes: int = 10) -> list[PoolWorker]:
    """Start deallocated pool VMs and wait for WAA ready."""
    for worker in pool.workers:
        vm_manager.start_vm(worker.name)
    # Wait for SSH (should be fast, ~1-2 min)
    # Start WAA containers (should detect ALREADY_RUNNING or restart)
    # Wait for WAA readiness (Windows warm boot = 1-2 min)
    return self.wait(timeout_minutes=timeout_minutes, start_containers=True)
```

New CLI commands in `vm_cli.py`:
```
oa-vm pool-pause       # Deallocate pool, stop billing
oa-vm pool-resume      # Start paused pool, wait for WAA
```

**Cost guardrails** (critical — user requirement):

1. **Auto-pause timer**: When pool is created or resumed, set an auto-pause countdown.
   Default: 2 hours. Configurable via `--auto-pause-hours`.
   ```python
   # In pool registry, store:
   pool.auto_pause_at = datetime.utcnow() + timedelta(hours=2)
   ```

2. **Cost tracking in pool registry**:
   ```python
   pool.cost_tracking = {
       "compute_started_at": "2026-02-23T10:00:00Z",
       "compute_cost_per_hour": 0.38,
       "disk_cost_per_day": 0.25,
       "total_compute_cost": 0.0,  # Updated on pause
       "total_disk_cost": 0.0,     # Updated on status check
       "paused_since": None,
   }
   ```

3. **Auto-cleanup for idle disks**: If pool stays paused for >7 days, warn on
   `pool-status`. After 14 days, auto-delete (with 24h warning via pool-status).
   ```
   $ oa-vm pool-status
   Pool: paused (13 days)
   WARNING: Pool will be auto-deleted in 1 day (14-day idle limit).
   Idle disk cost so far: $3.50
   To keep: oa-vm pool-resume
   To delete now: oa-vm pool-cleanup -y
   ```

4. **Resource tracker integration**: Extend `resource_tracker.py` to track
   deallocated VMs and their disk costs in RESOURCES.md.

5. **Session-start hook**: The existing resource tracker hook already alerts on
   running VMs. Extend to also show paused pools with accumulated disk cost.

**Time saved**: 35+ min on re-runs (from 42 min to ~5 min)

**Risks**:
- Forgotten paused pools accumulate $0.25/day → mitigated by auto-cleanup
- Static public IP may change on deallocate → use Azure Static IP (already default)
- Docker container state may be lost → WAA_START_SCRIPT handles restart

---

### 3. Windows Disk Persistence via Docker Volume (boot: 15-25 → 1-2 min)

**What**: The WAA container already mounts `-v /mnt/waa-storage:/storage` where `dockurr/windows` stores the QEMU disk image. If this volume persists across container restarts, Windows boots from the existing disk (1-2 min) instead of running OOBE (15-25 min).

**Current state**: The volume mount IS already in `WAA_START_SCRIPT`:
```bash
docker run ... -v /mnt/waa-storage:/storage ... waa-auto:latest
```

**What needs verification/fixing**:

1. **Container recreation**: `WAA_START_SCRIPT` does `docker rm -f winarena` before
   `docker run`. This destroys the container but `/mnt/waa-storage` persists on the
   host. The disk image should survive. Need to verify `dockurr/windows` reuses
   existing disk in `/storage` instead of creating a new one.

2. **Disk image path**: Verify that `dockurr/windows` stores the QEMU disk image
   at `/storage/data.img` (or similar) and detects it on startup.

3. **Pool-resume flow**: When VM is deallocated and restarted, `/mnt/waa-storage`
   persists (it's on the VM's temp disk `/mnt`). The container restart should find
   the existing Windows disk and boot from it.

**IMPORTANT**: `/mnt` is the Azure temp disk. It does NOT persist across
deallocate/start cycles on most VM sizes. This means:
- Warm start (VM just rebooted, not deallocated): `/mnt/waa-storage` persists ✓
- Cold start from deallocated state: `/mnt` may be wiped ✗

**Fix**: Use a persistent path instead of `/mnt`:

```python
# In WAA_START_SCRIPT, change:
# OLD: -v /mnt/waa-storage:/storage
# NEW: -v /home/azureuser/waa-storage:/storage

# This uses the OS disk which persists across deallocate/start
# Tradeoff: OS disk is smaller (30GB) but Windows disk is ~10-20GB
```

Alternatively, attach a separate Azure Managed Disk for persistence:
```bash
# One-time: create and attach a 64GB disk
az disk create -g openadapt-agents -n waa-pool-00-data --size-gb 64 --sku Premium_LRS
az vm disk attach -g openadapt-agents --vm-name waa-pool-00 -n waa-pool-00-data
# Mount inside VM:
sudo mkfs.ext4 /dev/sdc && sudo mount /dev/sdc /mnt/waa-persistent
```

**Recommended approach**: Use `/home/azureuser/waa-storage` on the OS disk. It's
simpler, persists across deallocate cycles, and the OS disk has enough space (30GB
OS disk → ~15GB free, Windows image is ~10GB). If space becomes an issue, upgrade
the OS disk to 64GB ($0.08/day incremental).

**Implementation**:

Update `WAA_START_SCRIPT` in `pool.py`:
```python
WAA_START_SCRIPT = """
# ...
docker run -d --name winarena \\
  ...
  -v /home/azureuser/waa-storage:/storage \\  # Changed from /mnt/waa-storage
  ...
"""
```

Update `DOCKER_SETUP_SCRIPT` in `pool.py`:
```python
# Change Docker data-root to OS disk too (for persistence)
# OR keep Docker on /mnt for speed but accept re-pull after deallocate
```

**Decision**: Keep Docker data-root on `/mnt` (fast temp disk) since we have the
pre-baked image (optimization #1) to avoid re-pulling. Only the Windows disk
needs to persist, and that goes on `/home/azureuser/waa-storage`.

**Time saved**: ~10-20 min (Windows OOBE only runs once per persistent disk)

**Cost**: $0 incremental (uses existing OS disk space)

---

### 4. Azure Container Registry for Docker Images (build: 5-10 → 1-2 min)

**What**: Push `waa-auto:latest` to the existing ACR (`openadaptacr.azurecr.io`)
instead of building it on every VM. VMs pull the pre-built image instead of
running `docker build`.

**Current state**: ACR already exists:
```
Name: openadaptacr
SKU: Basic
Location: eastus
Login: openadaptacr.azurecr.io
Admin: enabled
```

**Cost**: ACR Basic = **$0.17/day** ($5/month). Already being paid.

**Implementation**:

New commands:
```
oa-vm image-push      # Build waa-auto locally, push to ACR
oa-vm image-pull      # Pull waa-auto from ACR on a VM
```

Changes to `DOCKER_SETUP_SCRIPT` in `pool.py`:
```python
DOCKER_SETUP_SCRIPT_WITH_ACR = """
set -e
# ... (apt, docker install same as before) ...

# Authenticate to ACR
sudo docker login openadaptacr.azurecr.io -u openadaptacr -p {acr_password}

# Pull pre-built images from ACR (faster than building)
sudo docker pull openadaptacr.azurecr.io/waa-auto:latest
sudo docker tag openadaptacr.azurecr.io/waa-auto:latest waa-auto:latest

# Still pull base images (needed as cache layers)
sudo docker pull dockurr/windows:latest
"""
```

To push the image (one-time or on Dockerfile changes):
```python
def cmd_image_push(args):
    """Build and push waa-auto to ACR."""
    # 1. Build locally: docker build -t waa-auto:latest waa_deploy/
    # 2. Tag: docker tag waa-auto:latest openadaptacr.azurecr.io/waa-auto:latest
    # 3. Login: az acr login --name openadaptacr
    # 4. Push: docker push openadaptacr.azurecr.io/waa-auto:latest
```

**Note**: We can't build the Docker image locally (requires Docker with Linux
support for the multi-stage COPY from dockurr/windows). Instead, build on an
Azure VM first, then push to ACR:
```python
def cmd_image_push(args):
    """Push waa-auto from a running pool VM to ACR."""
    # 1. Get pool worker IP
    # 2. SSH: docker login openadaptacr.azurecr.io
    # 3. SSH: docker tag waa-auto:latest openadaptacr.azurecr.io/waa-auto:latest
    # 4. SSH: docker push openadaptacr.azurecr.io/waa-auto:latest
```

**Time saved**: 3-5 min (skip docker build on VM, just pull pre-built image)

**Cost**: $0 incremental (ACR Basic already exists and is being paid for)

---

## Combined Startup Scenarios

### Cold Start (new VM, no golden image)
Current: ~42 min. With ACR (#4): ~37 min.

### Cold Start (new VM, with golden image)
With #1 + #4: ~15 min (VM create + SSH + WAA container start + Windows OOBE).
With #1 + #3 + #4 (if golden image includes warm Windows disk): ~10 min.

### Warm Start (deallocated VM)
With #2 + #3: ~5 min (VM start + SSH + container restart + warm Windows boot).

### Hot Start (VM running, container stopped)
With #3: ~2 min (container restart + warm Windows boot).

---

## Cost Summary

| Resource | Condition | Cost | Auto-teardown |
|----------|-----------|------|---------------|
| VM compute | Running | $0.38/hr | Auto-shutdown after N hours |
| VM compute | Deallocated | $0.00 | N/A |
| OS Disk (30GB) | Deallocated VM | ~$0.15/day | Auto-delete after 14 days idle |
| Public IP | Deallocated VM | ~$0.10/day | Deleted with VM |
| Managed Image | Always | ~$0.01/day | Manual cleanup |
| ACR Basic | Always | $0.17/day | Already exists, shared |
| **Total idle (paused pool)** | | **~$0.25/day** | **Auto-delete after 14 days** |
| **Total idle (no pool)** | | **~$0.18/day** | Image + ACR only |

---

## Implementation Plan

### Phase 1: Deallocate + Resume (highest impact, lowest effort)

**Files to modify**:
| File | Changes |
|------|---------|
| `infrastructure/azure_vm.py` | Add `deallocate_vm()`, `start_vm()` methods |
| `infrastructure/pool.py` | Add `pause()`, `resume()` methods; cost tracking in registry |
| `benchmarks/vm_cli.py` | Add `pool-pause`, `pool-resume` commands |
| `infrastructure/vm_monitor.py` | Track `paused` pool state |
| `infrastructure/resource_tracker.py` | Show paused pools + disk cost in alerts |

**Estimated lines**: ~150 new, ~30 modified

### Phase 2: Windows Disk Persistence

**Files to modify**:
| File | Changes |
|------|---------|
| `infrastructure/pool.py` | Change `WAA_START_SCRIPT` volume mount path |

**Estimated lines**: ~5 modified (just the path change)

**Verification**: SSH into running VM, check `/home/azureuser/waa-storage/`
for Windows disk image after WAA boots. Restart container, verify Windows
boots in ~2 min.

### Phase 3: ACR Integration

**Files to modify**:
| File | Changes |
|------|---------|
| `infrastructure/pool.py` | Add ACR-aware `DOCKER_SETUP_SCRIPT` variant |
| `benchmarks/vm_cli.py` | Add `image-push` command |
| `config.py` | Add `acr_name`, `acr_login_server` settings |

**Estimated lines**: ~80 new, ~20 modified

### Phase 4: Pre-baked Managed Image

**Files to modify**:
| File | Changes |
|------|---------|
| `infrastructure/azure_vm.py` | Add `image_id` param to `create_vm()` |
| `infrastructure/pool.py` | Add `image` param to `create()`, skip Docker setup |
| `benchmarks/vm_cli.py` | Add `image-create`, `image-list`, `image-delete` commands |

**Estimated lines**: ~120 new, ~20 modified

### Phase 5: Auto-teardown + Cost Guardrails

**Files to modify**:
| File | Changes |
|------|---------|
| `infrastructure/pool.py` | Auto-pause timer, idle pool detection |
| `infrastructure/resource_tracker.py` | Paused pool cost tracking, stale pool warnings |
| `benchmarks/vm_cli.py` | `--auto-pause-hours` flag, idle cost display |

**Estimated lines**: ~100 new, ~30 modified

---

## Verification

1. `uv run pytest tests/ --ignore=tests/test_api_agent_ml.py -v` — no regressions
2. Manual test of pool-pause / pool-resume cycle
3. Verify Windows disk persistence across container restart
4. Verify ACR push/pull works
5. Verify golden image creation and VM boot from image
6. Verify auto-pause timer fires correctly
7. Verify resource tracker shows paused pool costs
