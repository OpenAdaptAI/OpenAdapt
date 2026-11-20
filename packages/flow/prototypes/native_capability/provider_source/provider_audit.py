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
    if not all(integer(scope[key]) for key in SCOPE_KEYS - {"runtime_id"}):
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


def audit(rows, expected_case, expected_pattern):
    """Refuse incomplete, contradictory, reset or content-bearing evidence."""
    if (
        not integer(expected_case, 0, 15)
        or not integer(expected_pattern, 0, 2)
        or type(rows) is not list
        or not 2 <= len(rows) <= 1025
    ):
        return False
    trace, result = rows[:-1], rows[-1]
    if (
        type(result) is not dict
        or set(result) != RESULT_KEYS
        or result["role"] != "result"
    ):
        return False
    if result["case"] != expected_case or result["pattern"] != expected_pattern:
        return False
    if not integer(result["case"], 0, 15) or not integer(result["pattern"], 0, 2):
        return False
    for key in (
        "bound",
        "acquire_attempted",
        "acquire_nonnull",
        "query_attempted",
        "query_nonnull",
        "trace_overflow",
        "trace_clean",
        "expected_outcome",
        "runtime_adoption",
    ):
        if type(result[key]) is not bool:
            return False
    for key in ("acquire_hr", "query_hr"):
        if not integer(result[key], -(2**31), 2**31 - 1):
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
        or result["forbidden"]
        or result["unexplained"]
        or result["live_objects"]
    ):
        return False
    if (
        not observation_valid(result)
        or not expected_result(result)
        or not result["expected_outcome"]
    ):
        return False
    if not scope_valid(result["oracle_scope"], runtime=False):
        return False
    if result["bound"]:
        observed = result["observed_scope"]
        if not scope_valid(observed, runtime=True):
            return False
        if any(
            observed[key] != result["oracle_scope"][key]
            for key in SCOPE_KEYS - {"runtime_id"}
        ):
            return False
    elif result["observed_scope"] is not None:
        return False
    counts, requested, returned = Counter(), Counter(), Counter()
    privacy_results, first_refusal, privacy_closed = [], None, False
    enters, accepted, current_privacy = [], [], []
    for ordinal, row in enumerate(trace):
        if type(row) is not dict or set(row) != TRACE_KEYS or row["role"] != "trace":
            return False
        if row["ordinal"] != ordinal or type(row["boolean_observed"]) is not bool:
            return False
        limits = {
            "event": (0, 20),
            "argument": (-(2**31), 2**31 - 1),
            "hr": (-(2**31), 2**31 - 1),
            "phase": (0, 6),
            "thread": (1, 2**32 - 1),
            "generation": (0, 2**64 - 1),
            "kind": (0, 65535),
            "boolean": (-32768, 32767),
            "ordinal": (0, 1023),
        }
        if any(not integer(row[key], *limit) for key, limit in limits.items()):
            return False
        event, argument = row["event"], row["argument"]
        if privacy_closed and not (
            event in (1, 8, 17) or (event == 11 and 300 < argument <= 309)
        ):
            return False
        if event == 20 and argument != -25:  # SDK UiaRootObjectId.
            return False
        counts[(event, argument)] += 1
        if event in (9, 10, 12, 13, 14):
            return False  # Semantic/action/error-string/Legacy/unknown calls never reset away.
        if event in (4, 16) and argument not in PROPERTIES:
            return False
        if event == 18 and row["hr"] != -2147418113:
            return False
        if event == 16:
            if row["boolean_observed"] != (row["hr"] == 0 and row["kind"] == 11):
                return False
            if argument == 30019:
                expected = {
                    4: (0, 11, -1),
                    5: (0, 13, 0),
                    6: (0, 3, 0),
                    7: (-2147467259, 0, 0),
                    8: (0, 0, 0),
                }.get(result["case"], (0, 11, 0))
                flip = {12: 0, 13: 1, 14: 2}.get(result["case"])
                if flip is not None and row["phase"] >= flip and row["phase"] < 5:
                    expected = (0, 11, -1)
                if (row["hr"], row["kind"], row["boolean"]) != expected:
                    return False
        if event in (5, 19) and abs(argument) not in PATTERNS:
            return False
        if event in (4, 5):
            requested[(event, argument)] += 1
        if event in (16, 19):
            returned[(4 if event == 16 else 5, abs(argument))] += 1
        if event == 11 and argument == 0:
            enters.append(row["phase"])
            current_privacy = []
        if event == 11 and argument == 200:
            if len(current_privacy) != 2 or any(
                privacy(r["hr"], r["kind"], r["boolean"]) != "non_sensitive"
                for r in current_privacy
            ):
                return False
            accepted.append(row["phase"])
        if event == 15:
            privacy_results.append(row)
            current_privacy.append(row)
            if privacy(row["hr"], row["kind"], row["boolean"]) != "non_sensitive":
                privacy_closed = True
            if row["argument"] != 100 or row["boolean_observed"] != (
                row["hr"] == 0 and row["kind"] == 11
            ):
                return False
        if event == 11 and 300 < argument <= 309:
            first_refusal = ordinal if first_refusal is None else first_refusal
        elif first_refusal is not None and event not in (1, 8, 17):
            return False  # Only owned cleanup after first refusal.
        if event == 17 and row["hr"] != 0:
            return False
    if [(r["event"], r["argument"]) for r in trace[:5]] != [(18, i) for i in range(5)]:
        return False
    if result["bound"] and len([r for r in privacy_results if r["phase"] == 5]) != 2:
        return False
    if result["guard_reason"]:
        stages = {11: [0], 12: [0], 13: [0, 1], 14: [0, 1, 2], 15: [0]}.get(
            result["case"], []
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
    if requested != returned or any(counts[(18, getter)] != 1 for getter in range(5)):
        return False
    if sum(count for (event, _), count in counts.items() if event == 18) != 5:
        return False
    if sum(count for (event, _), count in counts.items() if event == 0) != 1:
        return False
    if sum(count for (event, _), count in counts.items() if event == 1) != 1:
        return False
    if result["bound"] and not privacy_results:
        return False
    if result["guard_reason"] and first_refusal is None:
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
    return True
