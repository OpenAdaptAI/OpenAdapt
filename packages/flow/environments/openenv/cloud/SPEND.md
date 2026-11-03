# Spend ledger: parallel smoke test (AWS, account 992382684924, us-east-1)

The founder approved this test with a cap of $150 and a target under $50.
All resources were tagged `Project=openadapt`, `Purpose=openenv-parallel-smoke`
and `Owner=openenv-benches`, each with a one-day `Expires` tag.

Every instance was one-time spot, with a price cap ($1.00 or $1.20 per hour), a
45-minute self-termination timer, terminate-on-shutdown, a 30 GB gp3 root
volume deleted on termination, no key pair, no inbound rules and no IAM role.
Results came back through the serial console.

## 2026-11-03

| Instance | Type | UTC window | Minutes | Spot $/h | Cost (upper bound) | Note |
|---|---|---|---:|---:|---:|---|
| i-0415d35eccd7a4e42 | c6a.8xlarge spot | 23:30:29-23:32:48 | 2.3 | $0.4833 | $0.027 | boot log not captured (console write failed) |
| i-01d34bd68c7d3e63e | c7a.8xlarge spot | 23:35:33-23:38:30 | 3.0 | $0.5660 | $0.037 | boot log not captured |
| i-082dd21edadbddc42 | c7a.8xlarge spot | 23:40:07-23:43:26 | 3.3 | $0.5660 | $0.041 | found the cause: a getty on ttyS0 broke console writes |
| i-016c6f61475af7b0f | m6a.8xlarge spot | 23:45:42-23:48:57 | 3.2 | $0.6079 | $0.043 | kernel-log markers below console level |
| i-0a1da74bed9ceae5a | c7i.8xlarge spot | 23:52:37-23:55:32 | 2.9 | $0.8877 | $0.058 | result captured: N=100 passed |

**Total: under $0.21** in compute. Each instance's 30 GB gp3 volume
for a few minutes adds under $0.01. Two launch attempts were refused before
any instance started (spot vCPU quota while the previous instance released;
no capacity in one zone) and cost nothing.

Teardown, checked after the last run:
- all 5 instances terminated;
- no tagged volumes remain.

One spot request (`sir-g64fnn4j`) still showed as `active` because its
instance had just terminated. One-time requests close on their own and cost
nothing without an instance. Cancelling it explicitly was blocked by this
session's permissions and left to close on its own.
