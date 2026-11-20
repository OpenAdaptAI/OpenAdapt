"""Pure audit of future owned-provider category records; no native API/import."""

from collections import Counter

TRACE_KEYS = {
    "role",
    "ordinal",
    "event",
    "argument",
    "hr",
    "phase",
    "thread",
    "generation",
    "kind",
    "boolean_observed",
    "boolean",
}
RESULT_KEYS = {
    "role",
    "case",
    "pattern",
    "bound",
    "guard_reason",
    "capability",
    "capability_reason",
    "acquire_attempted",
    "acquire_hr",
    "acquire_nonnull",
    "query_attempted",
    "query_hr",
    "query_nonnull",
    "forbidden",
    "unexplained",
    "trace_overflow",
    "live_objects",
    "trace_clean",
    "expected_outcome",
    "observed_scope",
    "oracle_scope",
    "runtime_adoption",
    "trace_complete",
    "cleanup_complete",
}
SCOPE_KEYS = {
    "hwnd",
    "pid",
    "tid",
    "session",
    "creation_low",
    "creation_high",
    "generation",
    "runtime_id",
}
PATTERNS = {10015, 10010, 10002}  # SDK Toggle / SelectionItem / Value.
PROPERTIES = {30019, 30002, 30020, 30003, 30016, 30017}
NOT_SUPPORTED = -2147220988  # SDK UIA_E_NOTSUPPORTED, signed HRESULT.


def integer(value, low=0, high=2**64 - 1):
    return type(value) is int and low <= value <= high


def scope_valid(scope, *, runtime):
    if type(scope) is not dict or set(scope) != SCOPE_KEYS:
        return False
    if not all(
        integer(scope[key], 0, 2**32 - 1)
        for key in ("pid", "tid", "session", "creation_low", "creation_high")
    ):
        return False
    if not all(integer(scope[key]) for key in ("hwnd", "generation")):
        return False
    if any(scope[key] == 0 for key in ("hwnd", "pid", "tid", "generation")):
        return False
    if not (scope["creation_low"] or scope["creation_high"]):
        return False
    array = scope["runtime_id"]
    if type(array) is not list or len(array) > 64:
        return False
    if runtime != bool(array):
        return False
    return all(integer(item, -(2**31), 2**31 - 1) for item in array)


def privacy(status, kind, boolean):
    """Raw SDK VARIANT contract. No convenience bool/default/coercion."""
    if (
        not integer(status, -(2**31), 2**31 - 1)
        or status != 0
        or not integer(kind, 0, 65535)
        or kind != 11
    ):
        return "unknown"
    if type(boolean) is not int:
        return "unknown"
    return {0: "non_sensitive", -1: "sensitive"}.get(boolean, "unknown")


def observation_valid(result):
    acquire, query = result["acquire_attempted"], result["query_attempted"]
    ah, qh = result["acquire_hr"], result["query_hr"]
    ap, qp = result["acquire_nonnull"], result["query_nonnull"]
    capability, reason = result["capability"], result["capability_reason"]
    if not acquire and (ah != 0 or ap or query):
        return False
    if not query and (qh != 0 or qp):
        return False
    if capability == 1:
        return reason == 6 and acquire and query and ah == qh == 0 and ap and qp
    if capability == 2:
        return reason == 7 and acquire and ah == NOT_SUPPORTED and not ap and not query
    if capability != 0:
        return False
    if not result["bound"]:
        return reason == 0 and not acquire and not query
    if result["guard_reason"] != 0:
        return reason == 1
    if reason == 4:
        return acquire and ah == 0 and not ap and not query  # Normal null is Unknown.
    if reason == 2:
        return acquire and ah not in (0, NOT_SUPPORTED) and not query
    if reason == 3:
        return acquire and ah == NOT_SUPPORTED and ap and not query
    if reason == 5:
        return acquire and ah == 0 and ap and query and (qh != 0 or not qp)
    return False


def expected_result(result):
    case, bound, reason = result["case"], result["bound"], result["guard_reason"]
    acquire, query = result["acquire_attempted"], result["query_attempted"]
    capability = result["capability"]
    if case == 0:
        return bound and reason == 0 and capability == 1
    if case in (1, 2, 3):
        return bound and capability != 1
    if case == 4:
        return not bound and reason == 7
    if case in (5, 6, 7):
        return not bound and reason == 8
    if case == 8:
        return False  # VT_EMPTY permits host fallback; intent cannot certify Unknown.
    if case == 9:
        return not bound and reason == 3
    if case == 10:
        return not bound and reason == 6
    if case == 11:
        return bound and reason == 1 and not acquire
    if case == 12:
        return bound and reason == 7 and not acquire
    if case == 13:
        return bound and reason == 7 and acquire and not query
    if case == 14:
        return bound and reason == 7 and query and capability == 0
    return case == 15 and bound and reason == 3 and not acquire


def event_valid(row):
    if type(row) is not dict or set(row) != TRACE_KEYS or row["role"] != "trace":
        return False
    limits = {
        "event": (0, 23),
        "argument": (-(2**31), 2**31 - 1),
        "hr": (-(2**31), 2**31 - 1),
        "phase": (0, 6),
        "thread": (1, 2**32 - 1),
        "generation": (0, 2**64 - 1),
        "kind": (0, 65535),
        "boolean": (-32768, 32767),
        "ordinal": (0, 1023),
    }
    if (
        any(not integer(row[k], *limits[k]) for k in limits)
        or type(row["boolean_observed"]) is not bool
    ):
        return False
    event, arg, hr = row["event"], row["argument"], row["hr"]
    if event in (9, 10, 12, 13, 14):
        return False
    arguments = {
        0: {0},
        1: {0},
        2: {0},
        3: {0},
        6: set(range(6)),
        7: {0, 4},
        8: {0, 4},
        11: {0, 200, 400, *range(301, 310)},
        15: {100},
        17: {0, 1},
        18: set(range(5)),
        20: {-25},
        21: {0},
        22: set(range(4)),
        23: {0},
    }
    if event in (4, 16) and arg not in PROPERTIES:
        return False
    if event == 5 and arg not in PATTERNS:
        return False
    if event == 19 and abs(arg) not in PATTERNS:
        return False
    if event in arguments and arg not in arguments[event]:
        return False
    if event in (15, 16):
        if row["boolean_observed"] != (hr == 0 and row["kind"] == 11):
            return False
        if not row["boolean_observed"] and row["boolean"] != 0:
            return False
    elif row["kind"] != 0 or row["boolean_observed"] or row["boolean"] != 0:
        return False
    if event == 18:
        return hr == -2147418113
    if event in (15, 16, 19) or (event == 11 and 301 <= arg <= 309):
        return True
    return hr == 0


def audit(rows, expected_case, expected_pattern):
    if (
        not integer(expected_case, 0, 15)
        or not integer(expected_pattern, 0, 2)
        or type(rows) is not list
        or not 2 <= len(rows) <= 1025
    ):
        return False
    trace, result = rows[:-1], rows[-1]
    required = RESULT_KEYS | {"event_counts"}
    if (
        type(result) is not dict
        or set(result) != required
        or result["role"] != "result"
    ):
        return False
    if result["case"] != expected_case or result["pattern"] != expected_pattern:
        return False
    if not integer(result["case"], 0, 15) or not integer(result["pattern"], 0, 2):
        return False
    booleans = (
        "bound",
        "acquire_attempted",
        "acquire_nonnull",
        "query_attempted",
        "query_nonnull",
        "trace_overflow",
        "trace_clean",
        "expected_outcome",
        "runtime_adoption",
        "trace_complete",
        "cleanup_complete",
    )
    if any(type(result[key]) is not bool for key in booleans):
        return False
    if any(
        not integer(result[key], -(2**31), 2**31 - 1)
        for key in ("acquire_hr", "query_hr")
    ):
        return False
    for key, maximum in (
        ("guard_reason", 9),
        ("capability", 2),
        ("capability_reason", 7),
        ("forbidden", 1024),
        ("unexplained", 1024),
        ("live_objects", 1024),
    ):
        if not integer(result[key], 0, maximum):
            return False
    if (
        result["runtime_adoption"]
        or result["trace_overflow"]
        or not result["trace_clean"]
        or not result["trace_complete"]
        or not result["cleanup_complete"]
        or result["forbidden"]
        or result["unexplained"]
        or result["live_objects"]
    ):
        return False
    census = result["event_counts"]
    if (
        type(census) is not list
        or len(census) != 24
        or any(not integer(n, 0, 1024) for n in census)
    ):
        return False
    if (
        not observation_valid(result)
        or not expected_result(result)
        or not result["expected_outcome"]
    ):
        return False
    oracle = result["oracle_scope"]
    if not scope_valid(oracle, runtime=False) or oracle["generation"] != 1:
        return False
    if result["bound"]:
        observed = result["observed_scope"]
        if not scope_valid(observed, runtime=True):
            return False
        if any(observed[key] != oracle[key] for key in SCOPE_KEYS - {"runtime_id"}):
            return False
    elif result["observed_scope"] is not None:
        return False
    counts, pending, privacy_outputs = Counter(), {}, []
    enters, accepted, privacy_results = [], [], []
    privacy_open, current_privacy = False, []
    created, destroyed, final_destroyed = False, False, False
    first_refusal, privacy_closed = None, False
    pattern_outputs, pattern_qi = [], []
    cleanup = Counter()
    cleanup_order = []
    expected_id = (10015, 10010, 10002)[expected_pattern]
    last_generation = 0
    cleanup_events = {1, 8, 17, 21, 22, 23}
    for ordinal, row in enumerate(trace):
        if not event_valid(row) or row["ordinal"] != ordinal:
            return False
        event, arg, phase = row["event"], row["argument"], row["phase"]
        counts[event] += 1
        if row["generation"] < last_generation:
            return False
        last_generation = row["generation"]
        if event == 0:
            if created or destroyed or row["thread"] != oracle["tid"]:
                return False
            created = True
        if event == 1:
            if not created or destroyed or row["thread"] != oracle["tid"]:
                return False
            destroyed = True
        expected_generation = 2 if destroyed else 1 if created else 0
        if row["generation"] != expected_generation:
            return False
        if event == 23:
            if not destroyed or final_destroyed or row["thread"] != oracle["tid"]:
                return False
            final_destroyed = True
        if destroyed and event not in cleanup_events and event != 11:
            return False
        if (
            first_refusal is not None or privacy_closed
        ) and event not in cleanup_events:
            if not (event == 11 and 301 <= arg <= 309):
                return False
        if event in (4, 5):
            if not created or destroyed or arg in pending:
                return False
            if event == 4 and arg == 30019 and not privacy_open:
                return False  # Unrepresented bootstrap read is not a guard observation.
            if event == 5 and (arg != expected_id or phase != 0 or 0 not in accepted):
                return False
            pending[arg] = (event, phase, row["generation"])
        if event in (16, 19):
            key = abs(arg)
            expected_request = (4 if event == 16 else 5, phase, row["generation"])
            if pending.pop(key, None) != expected_request:
                return False
            if event == 16 and key == 30019:
                server = {
                    4: (0, 11, -1),
                    5: (0, 13, 0),
                    6: (0, 3, 0),
                    7: (-2147467259, 0, 0),
                    8: (0, 0, 0),
                }.get(expected_case, (0, 11, 0))
                flip = {12: 0, 13: 1, 14: 2}.get(expected_case)
                if flip is not None and flip <= phase < 5:
                    server = (0, 11, -1)
                if (row["hr"], row["kind"], row["boolean"]) != server:
                    return False
                privacy_outputs.append(row)
            elif event == 16:
                kind = 11 if key in (30016, 30017) else 3
                if (
                    row["hr"] != 0
                    or row["kind"] != kind
                    or (kind == 11 and row["boolean"] != -1)
                ):
                    return False
            else:
                server = {
                    1: (0, False),
                    2: (NOT_SUPPORTED, False),
                    3: (-2147467259, False),
                }.get(expected_case, (0, True))
                if (row["hr"], arg > 0) != server:
                    return False
                pattern_outputs.append(row)
        if event == 6 and arg in (1, 2, 3):
            if arg != expected_pattern + 1 or phase not in (0, 1):
                return False
            pattern_qi.append(row)
        if event == 11:
            if arg == 0:
                if phase > 4 or privacy_open:
                    return False
                enters.append(phase)
                current_privacy = []
            elif arg == 400:
                if privacy_open or phase > 5:
                    return False
                privacy_open = True
                privacy_outputs = []
            elif arg == 200:
                if phase > 4 or privacy_open or len(current_privacy) != 2:
                    return False
                if any(
                    privacy(r["hr"], r["kind"], r["boolean"]) != "non_sensitive"
                    for r in current_privacy
                ):
                    return False
                accepted.append(phase)
            else:
                if (
                    first_refusal is not None
                    or privacy_open
                    or arg - 300 != result["guard_reason"]
                ):
                    return False
                first_refusal = row
        if event == 15:
            if (
                not privacy_open
                or len(privacy_outputs) != 1
                or row["thread"] == oracle["tid"]
            ):
                return False
            privacy_open = False
            privacy_outputs = []
            privacy_results.append(row)
            current_privacy.append(row)
            if privacy(row["hr"], row["kind"], row["boolean"]) != "non_sensitive":
                privacy_closed = True
        if event in (21, 22):
            if (
                (event == 22 and not final_destroyed)
                or phase != 6
                or row["thread"] != oracle["tid"]
            ):
                return False
            if event == 22:
                if not cleanup[(21, 0)]:
                    return False
                if arg != 0:
                    cleanup_order.append(arg)
            cleanup[(event, arg)] += 1
    if pending or privacy_open or list(counts.get(i, 0) for i in range(24)) != census:
        return False
    if not (created and destroyed and final_destroyed):
        return False
    if cleanup_order != [1, 2, 3]:
        return False
    if (
        cleanup[(21, 0)] != 1
        or any(cleanup[(22, n)] != 1 for n in (1, 2, 3))
        or cleanup[(22, 0)] > 1
    ):
        return False
    if [(r["event"], r["argument"]) for r in trace[:5]] != [
        (18, i) for i in range(5)
    ] or counts[18] != 5:
        return False
    if result["bound"] and len([r for r in privacy_results if r["phase"] == 5]) != 2:
        return False
    if result["guard_reason"]:
        stages = {11: [0], 12: [0], 13: [0, 1], 14: [0, 1, 2], 15: [0]}.get(
            expected_case, []
        )
        expected_accepted = stages[:-1]
    elif result["capability"] == 1:
        stages, expected_accepted = [0, 1, 2, 3], [0, 1, 2, 3]
    elif result["capability"] == 2:
        stages, expected_accepted = [0, 4], [0, 4]
    elif result["query_attempted"]:
        stages, expected_accepted = [0, 1], [0, 1]
    else:
        stages, expected_accepted = [0], [0]
    if enters != stages or accepted != expected_accepted:
        return False
    if 1 in enters and (result["acquire_hr"] != 0 or not result["acquire_nonnull"]):
        return False
    if any(stage in enters for stage in (2, 3)) and (
        not result["query_attempted"]
        or result["query_hr"] != 0
        or not result["query_nonnull"]
    ):
        return False
    if 4 in enters and (
        result["acquire_hr"] != NOT_SUPPORTED or result["acquire_nonnull"]
    ):
        return False
    if bool(first_refusal) != bool(result["guard_reason"]):
        return False
    if result["guard_reason"] == 7 and not any(
        privacy(r["hr"], r["kind"], r["boolean"]) == "sensitive"
        for r in privacy_results
    ):
        return False
    if result["guard_reason"] == 8 and not any(
        privacy(r["hr"], r["kind"], r["boolean"]) == "unknown" for r in privacy_results
    ):
        return False
    if len(pattern_outputs) != int(result["acquire_attempted"]):
        return False
    if result["acquire_attempted"] and result["acquire_nonnull"] and not pattern_qi:
        return False
    if not result["acquire_nonnull"] and pattern_qi:
        return False
    return True
