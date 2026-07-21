# Policy Sync (unified, fail-closed)

The desktop app renders and enforces a single **effective policy** resolved by
the cloud control plane. Policy is layered in three tiers:

| Tier | Object   | Meaning                                   | Who may edit                          |
|------|----------|-------------------------------------------|---------------------------------------|
| 1    | `user`   | Per-user preferences                      | The local user (persisted via `set_config`) |
| 2    | `org`    | Org defaults                              | Org admins, **via the cloud API only** |
| 3    | `safety` | Safety guardrails that gate whether a run may proceed | Org admins, **via the cloud API only** |

The engine owns the **fetch + cache + fail-closed** half of this contract
(`engine/policy.py`, this PR). The frontend owns the **read-only rendering +
Tier-1 editing** half (`src/screens/Settings.tsx` on branch
`feat/tauri-frontend` — see the TODO section below; NOT built here).

---

## The contract: `GET /api/policy/effective`

Bearer-authed (the same `Authorization: Bearer <token>` the rest of the hosted
loop uses, resolved by `engine.auth.store.auth_header()`). The cloud returns the
org's fully-resolved policy:

```jsonc
{
  "policy_version": 7,                // int, monotonic per org
  "baseline_version": "2026.07",      // str, the safety baseline the org tracks
  "org_id": "org_42",                 // str
  "resolved_at": "2026-07-20T00:00:00Z", // iso8601
  "role": "owner" | "admin" | "member",
  "is_admin": true,                   // convenience bool derived from role

  "user": { "<tier1 key>": <value>, ... },  // Tier-1 per-user prefs (editable locally)
  "org":  { "<tier2 key>": <value>, ... },  // Tier-2 org defaults (read-only locally)

  "safety": {                         // Tier-3 -- ALWAYS fully populated by the server
    "effect_verification.required_for_consequential": true,
    "halt_on_ambiguous": true,
    "identity_gate.strictness": "strict",
    "pixel_verify.consequential_policy": "disabled",
    "unverified_write.allow": false,
    "egress.artifact_policy": "managed-strict",
    "model_calls.allowed_in_healthy_run": false
  }
}
```

### Why the payload carries `role` / `is_admin`

The engine knows the `org_id` (via the stored `Credential` / `AuthStatus`) but
has **no role or admin concept** of its own. Admin status is therefore carried
in the policy payload and surfaced by the module, so the frontend can decide
which cards are read-only. The engine never infers admin locally.

### Safety keys and their safest values

`engine.policy.SAFE_SAFETY_DEFAULTS` is the source of truth for the safest value
of every safety key. A missing or unreachable value **always resolves to these**:

| Key | Safe (fail-closed) value | Meaning of the safe value |
|-----|--------------------------|---------------------------|
| `effect_verification.required_for_consequential` | `true`  | Consequential actions must be effect-verified |
| `halt_on_ambiguous`                              | `true`  | Halt rather than guess when ambiguous |
| `identity_gate.strictness`                       | `"strict"` | Strictest identity matching |
| `pixel_verify.consequential_policy`              | `"disabled"` | Do not rely on pixel-only verification for consequential steps |
| `unverified_write.allow`                         | `false` | Never write without verification |
| `egress.artifact_policy`                         | `"managed-strict"` | Tightest artifact-egress posture |
| `model_calls.allowed_in_healthy_run`             | `false` | No model calls on a run the engine considers healthy |

---

## Cache location

The **raw** last-known-good response body is cached at:

```
~/.openadapt/policy.json
```

(the same `~/.openadapt/` directory as `config.toml`). Writes are **atomic**
(temp file in the same dir + `os.replace`), so a reader can never observe a
half-written file. The path is overridable in tests via
`OPENADAPT_POLICY_CACHE`.

The cache stores the raw server body **without** the synthetic `source` field —
`source` is added only on the in-memory resolved result.

---

## TTL / refresh policy

Policy is refreshed:

- **on app start** (warm the cache before the first render);
- **on a 300s (5-minute) interval** while the app runs; and
- **immediately before a run** (a run must never start on a stale safety view).

`refresh_policy` (dispatcher command) forces a network fetch and rewrites the
cache. `get_effective_policy` resolves through the normal network → cache →
fail-closed ladder.

---

## Fail-closed rule (the whole point)

Safety must never *weaken* because the network is down, the cache is stale, or
the server omitted a key.

1. **Resolution ladder** (`resolve_effective_policy`):
   `fetch_effective_policy` (network, `source="network"`) → on any failure
   `load_cached_policy` (`source="cache"`) → if neither exists, the fully
   fail-closed default (`source="fail-closed-default"`).
2. **Every** resolved policy passes through `harden_safety`, which guarantees
   each `SAFE_SAFETY_DEFAULTS` key is present. A **missing or `null`** safety
   value becomes the safe default. Values the server *did* provide are preserved
   (the server is authoritative when it speaks; we only fill gaps).
3. A **missing `safety` object** in a response is treated exactly like an empty
   one and hardened to all safe defaults.
4. Network and cache failures **never raise** out of `resolve_effective_policy`
   / `load_cached_policy` — they degrade (mirroring the keychain gate in
   `engine.auth.store`). Only `fetch_effective_policy` raises, so its caller can
   choose to fall back.
5. **A run whose engine cannot evaluate safety policy must refuse.** When
   `source == "fail-closed-default"` the engine has no authoritative safety
   view; the safest values still gate the run, and a consumer that requires a
   confirmed policy (e.g. a consequential run) should treat that as a hard stop
   rather than proceeding on defaults silently.

---

## Engine surface (implemented here)

| Symbol | Purpose |
|--------|---------|
| `engine/policy.py :: SAFE_SAFETY_DEFAULTS` | Safest value for every safety key |
| `engine/policy.py :: fetch_effective_policy(host, timeout=10.0)` | Bearer GET, atomic-cache-write, raises `PolicyFetchError` on failure |
| `engine/policy.py :: load_cached_policy()` | Read `~/.openadapt/policy.json`; `None` on any error (degrade-not-raise) |
| `engine/policy.py :: harden_safety(policy)` | Fill missing/`None` safety keys with safe defaults (fail-closed) |
| `engine/policy.py :: resolve_effective_policy(host)` | network → cache → fail-closed, always hardened, adds `source` |
| `engine/dispatch.py :: get_effective_policy` | Dispatcher command; never raises; returns the fail-closed default on error |
| `engine/dispatch.py :: refresh_policy` | Dispatcher command; forces a network fetch, then falls back like `get_effective_policy` |

Both commands are auto-exposed over the Tauri stdin/stdout wire (`engine/ipc.py`)
and the tray loopback (`engine/socket_server.py`) because they are registered in
`EngineDispatcher._register`.

---

## TODO — FRONTEND (NOT done here; lives on `feat/tauri-frontend`)

The read-only rendering + Tier-1 editing half of this contract is **not** part of
this PR because the real frontend lives on a different branch
(`feat/tauri-frontend`, worktree `.worktrees/app`). To complete the loop there:

1. **Add the command name** to `src/lib/engine.ts` `CMD` (e.g.
   `GET_EFFECTIVE_POLICY: "get_effective_policy"`, `REFRESH_POLICY: "refresh_policy"`)
   and a `Policy` TypeScript type mirroring the contract above (`safety` keys,
   `role`, `is_admin`, `source`).

2. **Extend `src/screens/Settings.tsx`** to call `get_effective_policy` on mount
   (and `refresh_policy` on demand), then render three sections reusing the
   existing primitives from `src/ui/primitives.tsx` — `Field`, `SegControl`, and
   `Callout` (all already imported in `Settings.tsx` today):

   - **Tier-1 `user`** → render as **editable** controls; persist each change via
     the existing `set_config` command (the same path the lane/PHI settings
     already use). These are per-user preferences.

   - **Tier-2 `org`** → render as **read-only** cards **unless** `policy.is_admin`.
     Admin edits go to the **cloud API**, never `set_config` — the desktop must
     not write org policy locally.

   - **Tier-3 `safety`** → render as **read-only** cards **unless**
     `policy.is_admin`; likewise admin edits go to the cloud API only. Use a
     `Callout` to explain when a card is locked (non-admin) and when the view is
     running on `source === "fail-closed-default"` (control plane unreachable —
     safest values are in force, and a consequential run should be blocked until
     policy can be confirmed).

3. **Surface `source`** in the UI (`network` / `cache` / `fail-closed-default`)
   so the user can tell whether they are looking at a live, cached, or
   safest-default policy.

The engine side (fetch, cache, fail-closed hardening, and the two dispatcher
commands) is fully implemented and tested in this PR; the frontend work above is
the only remaining piece of the unified policy-sync system.
