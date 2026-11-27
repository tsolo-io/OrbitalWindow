# SPDX-FileCopyrightText: 2026 Tsolo.io
#
# SPDX-License-Identifier: Apache-2.0

"""The AI usage report (#86)."""

from datetime import date

from orbitalwindow import usage

TODAY = date(2026, 11, 27)


def metrics(requests, tokens_in, tokens_out, failed=0, cached=0):
    """Return a LiteLLM ``metrics`` block.

    Args:
        requests: API requests.
        tokens_in: Prompt tokens.
        tokens_out: Completion tokens.
        failed: Failed requests.
        cached: Prompt tokens read from the cache.

    Returns:
        The block.
    """
    return {
        "api_requests": requests,
        "failed_requests": failed,
        "prompt_tokens": tokens_in,
        "completion_tokens": tokens_out,
        "cache_read_input_tokens": cached,
        "spend": 0.0,
    }


def day(when, total, keys=None, models=None, endpoints=None):
    """Return one day as ``/user/daily/activity`` gives it.

    Args:
        when: The day, as text.
        total: The day's metrics.
        keys: Metrics by user, put under made-up key hashes.
        models: Metrics by model.
        endpoints: Metrics by endpoint path.

    Returns:
        The day.
    """
    api_keys = {
        f"hash-{name}": {"metrics": block, "metadata": {"user_id": name, "key_alias": f"ow-{name}"}}
        for name, block in (keys or {}).items()
    }
    return {
        "date": when,
        "metrics": total,
        "breakdown": {
            "api_keys": api_keys,
            "models": {name: {"metrics": block} for name, block in (models or {}).items()},
            "endpoints": {name: {"metrics": block} for name, block in (endpoints or {}).items()},
        },
    }


CODER = "Qwen/Qwen2.5-Coder-3B"
DAYS = [
    day(
        "2026-11-27",
        metrics(3, 300, 30, failed=1),
        keys={"tayla": metrics(2, 200, 20, failed=1), "jsmith": metrics(1, 100, 10)},
        models={CODER: metrics(3, 300, 30, failed=1)},
        endpoints={
            "/v1/completions": metrics(2, 200, 20, failed=1),
            "/v1/chat/completions": metrics(1, 100, 10),
        },
    ),
    day(
        "2026-11-20",
        metrics(10, 1000, 100),
        keys={"jsmith": metrics(10, 1000, 100)},
        models={CODER: metrics(10, 1000, 100)},
        endpoints={"/v1/chat/completions": metrics(10, 1000, 100)},
    ),
    day("2026-10-01", metrics(99, 9, 9)),  # older than the history: left out
]


def test_history_has_every_day_also_without_use():
    """30 days, oldest first, ending today; days without use are 0."""
    report = usage.build_report(DAYS, TODAY, 1)
    assert len(report.days) == usage.HISTORY_DAYS
    assert report.days[0][0] == date(2026, 10, 29) and report.days[-1][0] == TODAY
    by_day = dict(report.days)
    assert by_day[TODAY].requests == 3 and by_day[TODAY].failed == 1
    assert by_day[date(2026, 11, 20)].tokens == 1100
    assert by_day[date(2026, 11, 21)].requests == 0


def test_models_by_the_name_clients_use():
    """Model groups (the session's model name) win over LiteLLM's provider-prefixed names."""
    row = day("2026-11-27", metrics(1, 1, 1), models={"openai/coder": metrics(1, 1, 1)})
    row["breakdown"]["model_groups"] = {"Qwen/Qwen2.5-Coder-3B": {"metrics": metrics(1, 1, 1)}}
    assert list(usage.build_report([row], TODAY, 1).by_model) == ["Qwen/Qwen2.5-Coder-3B"]
    del row["breakdown"]["model_groups"]
    assert list(usage.build_report([row], TODAY, 1).by_model) == ["openai/coder"]


def test_period_limits_the_breakdowns():
    """Today has only today's users; 7 days or more includes the 20th."""
    today = usage.build_report(DAYS, TODAY, 1)
    assert today.total.requests == 3
    assert {name: t.requests for name, t in today.by_user.items()} == {"tayla": 2, "jsmith": 1}
    week = usage.build_report(DAYS, TODAY, 7)
    assert week.total.requests == 3  # the 20th is 8 days ago
    month = usage.build_report(DAYS + [day("2026-11-25", metrics(1, 0, 0, failed=1))], TODAY, 30)
    assert month.total.requests == 14
    assert month.by_user["jsmith"].requests == 11
    assert month.by_model[CODER].tokens == 1430
    assert month.by_endpoint["Chat"].requests == 11
    assert month.by_endpoint["Code completion"].requests == 2
    # A failed request without an endpoint keeps the endpoint table's sum equal to the total.
    assert month.by_endpoint["Not known"].requests == 1
    assert month.by_endpoint["Not known"].failed == 1
    assert "Not known" not in week.by_endpoint


def test_endpoint_names_and_key_users():
    """Paths get the names users know; a key without user_id is found by its alias."""
    assert usage.endpoint_name("/v1/chat/completions") == "Chat"
    assert usage.endpoint_name("/chat/completions") == "Chat"
    assert usage.endpoint_name("/v1/completions") == "Code completion"
    assert usage.endpoint_name("/v1/embeddings") == "Embeddings"
    assert usage.endpoint_name("/v1/other") == "/v1/other"
    assert usage.key_user({"metadata": {"key_alias": "ow-tayla"}}) == "tayla"
    assert usage.key_user({"metadata": {"key_alias": "by-hand"}}) == "unknown"


def test_table_rows_most_tokens_first_with_share():
    """Rows are sorted by tokens, with the share of the period's tokens."""
    report = usage.build_report(DAYS, TODAY, 30)
    rows = usage.table_rows(report.by_user, report.total)
    assert [row["name"] for row in rows] == ["jsmith", "tayla"]
    assert rows[0]["share"] == "85 %" and rows[1]["share"] == "15 %"
    assert usage.share(5, 0) == "–"


def test_failed_rows_show_user_and_error():
    """A failed request shows who, which model, and the error."""
    logs = [
        {
            "user": "tayla",
            "model": "m",
            "call_type": "atext_completion",
            "startTime": "2026-11-27T07:00:00",
            "metadata": {"error_information": {"error_code": "401", "error_message": "blocked"}},
        },
        {"startTime": "2026-11-27T06:00:00", "metadata": {"user_api_key_user_id": "jsmith"}},
    ]
    rows = usage.failed_rows(logs)
    assert rows[0] == {
        "time": "2026-11-27T07:00:00",
        "user": "tayla",
        "model": "m",
        "endpoint": "Code completion",
        "error": "401 blocked",
    }
    assert rows[1]["user"] == "jsmith" and rows[1]["error"] == ""
    assert rows[1]["endpoint"] == "Not known"


def test_csv_has_all_tables():
    """The CSV has the days and the user, model and endpoint tables."""
    text = usage.to_csv(usage.build_report(DAYS, TODAY, 30))
    lines = text.splitlines()
    assert lines[0].startswith("table,name,requests")
    assert sum(line.startswith("day,") for line in lines) == usage.HISTORY_DAYS
    assert "user,jsmith,11,0,1100,110,0" in lines
    assert "endpoint,Code completion,2,1,200,20,0" in lines


def test_day_label():
    """Chart days are short."""
    assert usage.day_label(date(2026, 11, 7)) == "7 Nov"
