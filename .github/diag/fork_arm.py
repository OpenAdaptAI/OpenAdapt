"""One arm of the fork-stall experiment: start /usr/bin/true COUNT times."""
import os, subprocess, sys, tempfile, time

arm, count = sys.argv[1], int(sys.argv[2])
if arm in ("imports", "spawn", "hold"):
    import objc, Foundation, Quartz, AppKit  # noqa: F401,E401
    import mss, numpy, PIL.Image, httpx  # noqa: F401,E401
    import openadapt_desktop.dispatch  # noqa: F401
cwd = tempfile.mkdtemp()
out = subprocess.run(["/bin/ps", "-M", "-p", str(os.getpid())], capture_output=True, text=True, close_fds=False).stdout
print(f"arm={arm} pid={os.getpid()} native_threads={len(out.strip().splitlines()) - 1} modules={len(sys.modules)}", flush=True)
if arm == "hold":  # local check of the supervisor: a child held before exec
    subprocess.run(["/usr/bin/true"], preexec_fn=lambda: time.sleep(600))
    sys.exit(0)
start = time.time()
killed = 0
for i in range(1, count + 1):
    if arm == "spawn":
        done = subprocess.run(["/usr/bin/true"], capture_output=True, stdin=subprocess.DEVNULL, close_fds=False)
    else:
        done = subprocess.run(["/usr/bin/true"], capture_output=True, stdin=subprocess.DEVNULL, cwd=cwd)
    if done.returncode != 0:
        killed += 1
        print(f"iteration {i}: child ended with {done.returncode} after a stall", flush=True)
    if i % 20000 == 0:
        print(f"arm={arm} {i} started in {time.time() - start:.0f}s, stalls so far {killed}", flush=True)
print(f"RESULT arm={arm} count={count} stalls={killed} seconds={time.time() - start:.0f}", flush=True)
