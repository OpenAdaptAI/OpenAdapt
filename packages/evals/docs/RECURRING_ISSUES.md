# Recurring Issues Registry

**Purpose**: Prevent solving the same problem multiple times due to context loss.

**Rule**: Before fixing ANY infrastructure issue, check this file and run `bd list --labels=recurring`.

---

## Issue #1: Windows Product Key / Edition Selection Prompt

**Symptom**: Windows installer shows "Select operating system" or asks for product key instead of auto-installing.

**Root Cause** (RCA completed 2026-01-20):

**VERSION MISMATCH between Dockerfile and CLI:**
- Dockerfile (line 275): `VERSION="11e"`
- CLI (lines 3273, 6110, 6180): `VERSION=11`

Dockerfile patches XML for 11e, but runtime uses 11's XML (unpatched).

**Correct understanding:**
- `VERSION=11e` (Enterprise Eval) = Has built-in GVLK key, **NEVER prompts**
- `VERSION=11` (Pro) = May prompt for product key if XML incorrect

**NOTE**: Earlier working versions used volume licensing - check git history and README screenshots for working Azure instances.

**Fix Checklist** (VERIFIED):
- [ ] CLI uses `VERSION=11e` in ALL 3 places (cli.py lines 3273, 6110, 6180)
- [ ] Dockerfile uses `VERSION=11e` (already correct)
- [ ] CLAUDE.md documentation is correct (was backwards)
- [ ] Delete cached storage: `rm -f /data/waa-storage/data.img`
- [ ] Rebuild if Dockerfile changed: `docker build --no-cache -t waa-auto .`

**Prior Fix Attempts**:
| Date | Commit | What was tried | Result | Why it failed |
|------|--------|----------------|--------|---------------|
| ~Jan 15 | ??? | Added InstallFrom to XML | Partial | Only patched one XML file |
| ~Jan 17 | ??? | Used VERSION=11 | Broke it | Introduced mismatch with Dockerfile |
| ~Jan 18 | ??? | Patched BOTH XML files | Should work | But CLI still passes VERSION=11 |
| Jan 20 | ??? | Reset storage + fresh install | Still broken | Didn't fix VERSION mismatch |

**Full RCA**: `/Users/abrichr/oa/src/openadapt-ml/docs/WINDOWS_PRODUCT_KEY_RCA.md`

**Beads Tasks**: `bd list --labels=windows,product-key`

---

## Issue #2: WAA Server Not Responding (Timeout)

**Symptom**: `vm probe` times out after 600s, WAA server never responds on port 5000.

**Root Cause**: Multiple factors:
1. Windows stuck at installation prompt (see Issue #1)
2. Windows installed but install.bat never ran (FirstLogonCommands failed)
3. Python/Flask not installed in Windows
4. Network misconfiguration (wrong IP: should be 172.30.0.2)

**Fix Checklist**:
- [ ] Check VNC at localhost:8006 - what does Windows show?
- [ ] If at desktop, check if install.bat ran (look for WAA folder)
- [ ] Check container logs: `docker logs winarena`
- [ ] Verify network: container should use `--net=waa-net` with IP 172.30.0.2

**Beads Tasks**: `bd list --labels=waa,timeout`

---

## Adding New Recurring Issues

When you encounter an issue that:
1. Has happened before (check git history, CLAUDE.md)
2. Involves infrastructure (VMs, Docker, Windows, Azure)
3. Has non-obvious root causes

Add it here with:
1. **Symptom**: What the user sees
2. **Root Cause**: Why it happens (may be multiple)
3. **Fix Checklist**: Step-by-step verification
4. **Prior Fix Attempts**: Table of what was tried

Also create a Beads task with `--labels=recurring`:
```bash
bd create "Issue title" -p 1 --labels=recurring,infrastructure -d "Description"
```

---

## For Claude Code Agents

**MANDATORY before any infrastructure fix**:
1. Read this file
2. Run `bd list --labels=recurring`
3. Check if this issue is already documented
4. If yes, follow the Fix Checklist
5. If no, document it here BEFORE attempting fix

**After any fix attempt**:
1. Update the "Prior Fix Attempts" table
2. Create/update Beads task with result
3. If fix worked, document WHY in root cause section
