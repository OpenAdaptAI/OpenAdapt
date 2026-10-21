"""Exit 0 once the app shows more protected-page views than BASELINE."""

import json
import sys
import urllib.request

origin, baseline = sys.argv[1], int(sys.argv[2])
with urllib.request.urlopen(origin + "/status", timeout=3) as resp:
    views = json.loads(resp.read())["protected_views"]
sys.exit(0 if views > baseline else 1)
