"""Run one arm and sample any child of it that lives longer than a moment."""
import os, subprocess, sys, time

python, arm, count, outdir = sys.argv[1:5]
os.makedirs(outdir, exist_ok=True)
STUCK_AFTER = 20.0


def run(cmd, timeout=60):
    try:
        return subprocess.run(cmd, capture_output=True, text=True, close_fds=False, timeout=timeout).stdout
    except Exception as exc:  # noqa: BLE001
        return f"{cmd[0]} failed: {exc!r}\n"


def children(parent):
    rows = []
    for line in run(["/bin/ps", "-axo", "pid=,ppid=,command="]).splitlines():
        parts = line.split(None, 2)
        if len(parts) == 3 and parts[1] == str(parent):
            rows.append((int(parts[0]), parts[2]))
    return rows


here = os.path.dirname(os.path.abspath(__file__))
proc = subprocess.Popen([python, os.path.join(here, "fork_arm.py"), arm, count], close_fds=False)
seen: dict[int, float] = {}
stalls = 0
while proc.poll() is None:
    time.sleep(2)
    now = time.time()
    current = dict(children(proc.pid))
    for pid in list(seen):
        if pid not in current:
            del seen[pid]
    for pid, command in current.items():
        first = seen.setdefault(pid, now)
        if now - first < STUCK_AFTER:
            continue
        stalls += 1
        print(f"\n===== STALL {stalls}: child {pid} of {proc.pid} alive {now - first:.0f}s: {command[:120]}", flush=True)
        for label, target in (("child", pid), ("parent", proc.pid)):
            threads = run(["/bin/ps", "-M", "-p", str(target)])
            sample = run(["/usr/bin/sample", str(target), "3", "-mayDie"], timeout=120)
            path = os.path.join(outdir, f"{arm}-stall{stalls}-{label}.txt")
            with open(path, "w") as handle:
                handle.write(threads + "\n" + sample)
            print(f"--- {label} {target}: ps -M\n{threads}", flush=True)
            print(f"--- {label} {target}: sample (call graph)\n" + sample[:6000], flush=True)
        try:
            os.kill(pid, 9)
        except ProcessLookupError:
            pass
        del seen[pid]
print(f"SUPERVISOR arm={arm} stalls_sampled={stalls} arm_exit={proc.returncode}", flush=True)
