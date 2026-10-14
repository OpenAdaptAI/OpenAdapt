#!/bin/bash
# Start the evaluate server in background (runs on Docker Linux side)
# The evaluate server provides /evaluate and /task/<id> endpoints using
# WAA's evaluator modules from /client/desktop_env/evaluators/

EVAL_SERVER="/evaluate_server.py"

# Validate evaluate_server.py before starting
if [ -L "$EVAL_SERVER" ]; then
    echo "ERROR: $EVAL_SERVER is a symlink to $(readlink "$EVAL_SERVER"), not a real file." >&2
    echo "The Docker image was built incorrectly. Rebuild with evaluate_server.py in the build context." >&2
elif [ ! -f "$EVAL_SERVER" ]; then
    echo "ERROR: $EVAL_SERVER does not exist." >&2
    echo "The Docker image is missing the evaluate server. Rebuild the image." >&2
elif [ ! -s "$EVAL_SERVER" ]; then
    echo "ERROR: $EVAL_SERVER is empty (0 bytes)." >&2
    echo "The Docker image was built with a corrupt evaluate_server.py. Rebuild the image." >&2
elif ! grep -q '/probe' "$EVAL_SERVER" || ! grep -q '/evaluate' "$EVAL_SERVER"; then
    echo "ERROR: $EVAL_SERVER is missing expected Flask routes (/probe, /evaluate)." >&2
    echo "File content (first 5 lines):" >&2
    head -5 "$EVAL_SERVER" >&2
    echo "The file may be corrupt. Rebuild the image." >&2
else
    cd /client
    python "$EVAL_SERVER" > /tmp/evaluate_server.log 2>&1 &
    echo "Evaluate server started on port 5050 (PID: $!)"

    # Exempt port 5050 from the DNAT rule that forwards all traffic to
    # the Windows VM. Without this, connections to port 5050 get forwarded
    # to 172.30.0.2:5050 (Windows) instead of reaching the evaluate server
    # running on the container's Linux side.
    #
    # The DNAT rule for Windows is set up by network.sh which can run at
    # various times during boot. A single sleep+apply is unreliable — the
    # Windows DNAT rule can be (re)applied AFTER our exemption.  Instead,
    # we apply the exemption repeatedly for the first 2 minutes to ensure
    # it always sits above any DNAT rule in the chain.
    (
        for attempt in $(seq 1 12); do
            sleep 10
            iptables -t nat -C PREROUTING -p tcp --dport 5050 -j ACCEPT 2>/dev/null \
                && continue  # already in place
            iptables -t nat -I PREROUTING 1 -p tcp --dport 5050 -j ACCEPT 2>/dev/null \
                && echo "iptables: exempted port 5050 from DNAT (attempt $attempt)" \
                || echo "iptables: failed to exempt port 5050 (attempt $attempt, non-fatal)"
        done
    ) &
fi

# Execute the command passed as arguments (e.g., /entry.sh --prepare-image false)
# If no CMD is provided (empty $@), fall through to /run/entry.sh which is the
# dockurr/windows entrypoint that boots the Windows VM.
if [ $# -eq 0 ]; then
    echo "No CMD specified, using default: /run/entry.sh"
    exec /run/entry.sh
else
    exec "$@"
fi
