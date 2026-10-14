# AWS Spot Instance Analysis for GPU Training

**Date**: 2026-03-04
**Context**: Cost optimization for verl-agent/VAGEN RL training on AWS GPU instances

## Spot Pricing vs On-Demand

### Single-GPU (Development/Validation)

| Instance | GPU | VRAM | On-Demand | Spot | Savings |
|----------|-----|------|-----------|------|---------|
| g5.xlarge | 1x A10G | 24GB | $1.006/hr | $0.43/hr | 57% |
| g5.2xlarge | 1x A10G | 24GB | $1.21/hr | ~$0.52/hr | 57% |
| g6.xlarge | 1x L4 | 24GB | $0.805/hr | $0.38-0.55/hr | 31-53% |

### Multi-GPU (Production Training)

| Instance | GPUs | VRAM | On-Demand | Spot | Savings |
|----------|------|------|-----------|------|---------|
| g5.12xlarge | 4x A10G | 96GB | $5.67/hr | $2.90/hr | 49% |
| g6.12xlarge | 4x L4 | 96GB | $4.60/hr | $2.26/hr | 51% |

**g6 (NVIDIA L4) is a viable alternative** to g5 (A10G) — both have 24GB VRAM per GPU. L4 has better inference performance, which suits RL's rollout-heavy workload.

## Cost Projections

### 7-Day Training Run (Multi-GPU)

| Strategy | Hourly | 7-Day | vs On-Demand |
|----------|--------|-------|-------------|
| On-Demand g5.12xlarge | $5.67 | $953 | baseline |
| Spot g5.12xlarge | $2.90 | $488 | -49% |
| Spot g6.12xlarge | $2.26 | $380 | -60% |

With ~7% interruption rate and 15-min checkpoints, expect ~10-15% overhead from re-computation. Adjusted real savings: 42-55%.

## Interruption Risk

- AWS overall: 95% of spot instances run to completion
- GPU instances: estimated 5-10% interruption rate in trailing month
- **AZ-level variance is significant** — some AZs >20% while others <5%
- 2-minute termination warning via instance metadata
- For a 24-hour run with ~7% rate: expect 1-2 interruptions

## Recommendations

1. **Start with spot for dev/validation** (g5.xlarge at $0.43/hr)

2. **For multi-GPU production training**, use EC2 Fleet with:
   - Instance types: g5.12xlarge + g6.12xlarge (diversification)
   - Allocation strategy: `price-capacity-optimized`
   - Regions: us-east-1 primary, us-east-2 fallback

3. **Checkpoint to S3 every 15 minutes** — do NOT rely on EBS survival

4. **Add termination handler** — poll instance metadata every 5s, trigger immediate checkpoint on 2-min warning

5. **Set `DeleteOnTermination=false`** on EBS volumes, or better yet, use S3 for all checkpoints

## Gotchas

- **EBS deleted on spot termination by default** — must change or use S3
- **p3 (V100) incompatible** with OSS NVIDIA driver (needs GSP/Ampere+)
- **g4dn (T4) only 16GB VRAM** — likely insufficient for VLM RL
- **SageMaker Managed Spot** adds 15-40% markup, not recommended for custom verl-agent loops
- **Reattaching EBS across AZs requires snapshots** — S3 checkpoints avoid this entirely

## Implementation TODO

- [x] Add spot instance support to `aws_vm.py` (`create_vm` with `InstanceMarketOptions`)
- [ ] Add S3 checkpoint upload to training loop
- [ ] Add termination handler (metadata polling + checkpoint trigger)
- [ ] Add g6 instance types to `GPU_INSTANCE_TYPE_FALLBACKS` in `aws_vm.py`
- [ ] Test EC2 Fleet with `price-capacity-optimized` allocation
