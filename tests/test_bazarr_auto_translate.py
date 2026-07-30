"""Tests for the subtitle processing logic.

The script filename contains hyphens, so it cannot be imported by name and has
to be loaded through importlib. Importing it is side-effect free: module scope
only reads environment variables, configures logging and builds a Session.
"""

import importlib.util
import logging
import os
import signal
import sys
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path

import pytest
import requests

SCRIPT = Path(__file__).resolve().parent.parent / "bazarr-auto-translate.py"

MOVIE = {"radarrId": 1, "title": "Test Movie"}
EPISODE = {"sonarrEpisodeId": 2, "sonarrSeriesId": 3, "seriesTitle": "Test Series"}

# Read at module scope by the script, so they decide what the tests observe.
CONFIG_VARS = (
    "BAZARR_HOSTNAME",
    "BAZARR_PORT",
    "BAZARR_APIKEY",
    "CRON_SCHEDULE",
    "FIRST_LANG",
    "RUN_NOW",
    "REQUEST_TIMEOUT",
    "TRANSLATE_DELAY",
    "MAX_RETRIES",
    "INITIAL_BACKOFF",
    "MAX_BACKOFF",
    "RUN_DEADLINE",
    "STATE_DIR",
    "LOG_LEVEL",
    "BAZARR_SCHEME",
)


def _load_module(monkeypatch=None):
    spec = importlib.util.spec_from_file_location("bazarr_auto_translate", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    if monkeypatch is None:
        sys.modules[spec.name] = module
    else:
        monkeypatch.setitem(sys.modules, spec.name, module)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def clean_env(monkeypatch):
    """Drop every config var the script reads at import.

    Without this a developer with FIRST_LANG=en exported sees unrelated
    failures, and a malformed REQUEST_TIMEOUT takes the whole suite down.
    """
    for var in CONFIG_VARS:
        monkeypatch.delenv(var, raising=False)
    return monkeypatch


@pytest.fixture
def raw(clean_env):
    """The module under test with a clean environment and nothing stubbed."""
    return _load_module(clean_env)


@pytest.fixture
def bat(raw, monkeypatch):
    """The module with every outbound call stubbed out."""

    def unexpected(*args, **kwargs):
        raise AssertionError("the test made an unstubbed HTTP request")

    monkeypatch.setattr(raw.session, "request", unexpected)
    monkeypatch.setattr(raw, "download_subtitles", lambda *a, **kw: None)
    monkeypatch.setattr(raw, "get_subtitles_info", lambda *a, **kw: None)
    monkeypatch.setattr(raw, "make_api_request", lambda *a, **kw: None)
    monkeypatch.setattr(raw, "TRANSLATE_DELAY", 5)
    return raw


@pytest.fixture
def translated(bat, monkeypatch):
    """Records the arguments of every translate_subtitles call."""
    calls = []

    def fake(*args):
        calls.append(args)
        return {"status": "ok"}

    monkeypatch.setattr(bat, "translate_subtitles", fake)
    return calls


@pytest.fixture
def sleeps(bat, monkeypatch):
    """Records every pause instead of taking it.

    False means "not shutting down", matching what _sleep returns.
    """
    recorded = []
    monkeypatch.setattr(
        bat, "_sleep", lambda seconds: recorded.append(seconds) or False
    )
    return recorded


def stub_info(bat, monkeypatch, *responses):
    """Queue up successive get_subtitles_info responses.

    Strict on purpose: process_subtitles can look up twice, and a stub that
    silently returned None for the second call let tests pass through a branch
    they were not describing.
    """
    queue = list(responses)

    def fake(media_type, **params):
        if not queue:
            raise AssertionError("get_subtitles_info called more times than queued")
        return queue.pop(0)

    monkeypatch.setattr(bat, "get_subtitles_info", fake)


def stub_wanted(bat, monkeypatch, payload):
    monkeypatch.setattr(bat, "make_api_request", lambda *a, **kw: payload)


def with_subs(*subtitles):
    return {"data": [{"subtitles": list(subtitles)}]}


# process_subtitles: responses that used to crash the daemon


def test_empty_data_list_is_handled(bat, monkeypatch, translated):
    """Regression: data[0] on an empty list raised IndexError and killed the run."""
    stub_info(bat, monkeypatch, {"data": []})
    assert bat.process_subtitles(MOVIE, "movies") == bat.FAILED
    assert translated == []


def test_entry_without_subtitles_key_is_handled(bat, monkeypatch, translated):
    """Both lookups queued: the second exercises the same guard after a download."""
    stub_info(bat, monkeypatch, {"data": [{}]}, {"data": [{}]})
    assert bat.process_subtitles(MOVIE, "movies") == bat.NO_SOURCE
    assert translated == []


def test_null_response_is_handled(bat, monkeypatch, translated):
    stub_info(bat, monkeypatch, None)
    assert bat.process_subtitles(MOVIE, "movies") == bat.FAILED
    assert translated == []


def test_subtitle_without_code2_is_handled(bat, monkeypatch, translated):
    subs = with_subs({"path": "/movie.srt"})
    stub_info(bat, monkeypatch, subs, subs)
    assert bat.process_subtitles(MOVIE, "movies") == bat.NO_SOURCE
    assert translated == []


# The ID guard: a dropped param widens the query to the whole library, so the
# proof is that nothing goes out, not that the return value happens to be False.


@pytest.mark.parametrize(
    "item,media_type",
    [
        pytest.param({"title": "No ID"}, "movies", id="movie-without-radarr-id"),
        pytest.param(
            {"sonarrEpisodeId": 2, "seriesTitle": "S"},
            "episodes",
            id="episode-without-series-id",
        ),
        pytest.param(
            {"sonarrSeriesId": 3, "seriesTitle": "S"},
            "episodes",
            id="episode-without-episode-id",
        ),
    ],
)
def test_item_without_usable_id_sends_nothing(
    bat, monkeypatch, translated, item, media_type
):
    downloads = []
    monkeypatch.setattr(bat, "download_subtitles", lambda *a, **kw: downloads.append(a))
    monkeypatch.setattr(
        bat,
        "get_subtitles_info",
        lambda *a, **kw: pytest.fail("looked up an unscoped item"),
    )
    assert bat.process_subtitles(item, media_type) == bat.FAILED
    assert downloads == []
    assert translated == []


# process_subtitles: the return value that gates the delay


def test_existing_target_language_skips_translation(bat, monkeypatch, translated):
    stub_info(
        bat, monkeypatch, with_subs({"code2": bat.FIRST_LANG, "path": "/movie.pl.srt"})
    )
    assert bat.process_subtitles(MOVIE, "movies") == bat.SATISFIED
    assert translated == []


def test_english_subtitles_trigger_translation(bat, monkeypatch, translated):
    stub_info(bat, monkeypatch, with_subs({"code2": "en", "path": "/movie.en.srt"}))
    assert bat.process_subtitles(MOVIE, "movies") == bat.TRANSLATED
    assert translated == [("/movie.en.srt", bat.FIRST_LANG, "movie", 1)]


def test_english_downloaded_on_second_lookup(bat, monkeypatch, translated):
    stub_info(
        bat, monkeypatch, with_subs(), with_subs({"code2": "en", "path": "/ep.en.srt"})
    )
    assert bat.process_subtitles(EPISODE, "episodes") == bat.TRANSLATED
    assert translated == [("/ep.en.srt", bat.FIRST_LANG, "episode", 2)]


# translate_wanted: pacing and batch resilience


def test_no_delay_when_nothing_was_translated(bat, monkeypatch, sleeps):
    """Regression: the delay fired between every pair of items regardless."""
    stub_wanted(bat, monkeypatch, {"total": 3, "data": [MOVIE, MOVIE, MOVIE]})
    monkeypatch.setattr(
        bat, "process_subtitles", lambda item, media_type: bat.SATISFIED
    )
    bat.translate_wanted("movies", {})
    assert sleeps == []


def test_delay_between_translated_items(bat, monkeypatch, sleeps):
    stub_wanted(bat, monkeypatch, {"total": 3, "data": [MOVIE, MOVIE, MOVIE]})
    monkeypatch.setattr(
        bat, "process_subtitles", lambda item, media_type: bat.TRANSLATED
    )
    bat.translate_wanted("movies", {})
    assert sleeps == [5, 5]


def test_no_delay_after_the_last_item(bat, monkeypatch, sleeps):
    stub_wanted(bat, monkeypatch, {"total": 1, "data": [MOVIE]})
    monkeypatch.setattr(
        bat, "process_subtitles", lambda item, media_type: bat.TRANSLATED
    )
    bat.translate_wanted("movies", {})
    assert sleeps == []


def test_wanted_without_data_key_is_handled(bat, monkeypatch, sleeps):
    """total > 0 with no data list used to raise KeyError."""
    stub_wanted(bat, monkeypatch, {"total": 3})
    processed = []
    monkeypatch.setattr(
        bat,
        "process_subtitles",
        lambda item, media_type: processed.append(item) or bat.SATISFIED,
    )
    bat.translate_wanted("movies", {})
    assert processed == []
    assert sleeps == []


def test_episodes_are_processed_too(bat, monkeypatch, sleeps):
    stub_wanted(bat, monkeypatch, {"total": 1, "data": [EPISODE]})
    seen = []

    def record(item, media_type):
        seen.append(media_type)
        return bat.SATISFIED

    monkeypatch.setattr(bat, "process_subtitles", record)
    bat.translate_wanted("episodes", {})
    assert seen == ["episodes"]


# Response-shape helpers


@pytest.mark.parametrize(
    "response", [None, {}, {"data": None}, {"data": {}}, {"data": "x"}]
)
def test_entries_rejects_non_list_data(bat, response):
    assert bat._entries(response) == []


def test_entries_returns_the_list(bat):
    assert bat._entries({"data": [1, 2]}) == [1, 2]


def test_find_sub_ignores_entries_without_a_path(bat):
    subs = [{"code2": "en"}, {"code2": "en", "path": "/real.srt"}]
    assert bat._find_sub(subs, "en") == {"code2": "en", "path": "/real.srt"}
    assert bat._find_sub(subs, "de") is None


def test_get_current_subs_distinguishes_failure_from_empty(bat, monkeypatch):
    """None means the lookup failed; [] means Bazarr has no subtitles yet."""
    stub_info(bat, monkeypatch, {"data": []}, {"data": [{}]})
    assert bat.get_current_subs("movies", {"radarrid": 1}) is None
    assert bat.get_current_subs("movies", {"radarrid": 1}) == []


def test_get_current_subs_takes_the_first_entry(bat, monkeypatch):
    """The query is scoped to one item, so anything past the first is not ours."""
    stub_info(
        bat,
        monkeypatch,
        {
            "data": [
                {"subtitles": [{"code2": "en", "path": "/ours.srt"}]},
                {"subtitles": [{"code2": "en", "path": "/someone-elses.srt"}]},
            ]
        },
    )
    assert bat.get_current_subs("movies", {"radarrid": 1}) == [
        {"code2": "en", "path": "/ours.srt"}
    ]


def test_get_current_subs_sends_bazarr_array_params(bat, monkeypatch):
    """Bazarr needs the ids as repeated `key[]` params, not plain keys."""
    seen = {}

    def record(media_type, **params):
        seen.update(params)
        return {"data": [{}]}

    monkeypatch.setattr(bat, "get_subtitles_info", record)
    bat.get_current_subs("episodes", {"seriesid": 3, "episodeid": 2})
    assert seen == {"seriesid[]": 3, "episodeid[]": 2}


# Environment parsing


def test_negative_translate_delay_is_clamped(clean_env):
    """A negative delay would raise ValueError from time.sleep mid-batch."""
    clean_env.setenv("TRANSLATE_DELAY", "-5")
    clean_env.setenv("MAX_RETRIES", "-1")
    module = _load_module(clean_env)
    assert module.TRANSLATE_DELAY == 0
    assert module.MAX_RETRIES == 0


# make_api_request retry and backoff


class FakeResponse:
    def __init__(self, status=200, payload=None, content=b"{}", headers=None):
        self.status_code = status
        self.content = content
        self.headers = headers or {}
        self._payload = payload

    def json(self):
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.exceptions.HTTPError(
                f"HTTP {self.status_code}", response=self
            )


@pytest.fixture
def transport(raw, monkeypatch):
    """Drive make_api_request from the socket up, recording calls and sleeps."""
    calls, naps = [], []
    monkeypatch.setattr(raw, "_sleep", lambda seconds: naps.append(seconds) or False)
    monkeypatch.setattr(raw.random, "uniform", lambda a, b: 0)
    monkeypatch.setattr(raw, "INITIAL_BACKOFF", 60)
    monkeypatch.setattr(raw, "MAX_BACKOFF", 300)

    def install(*responses):
        queue = list(responses)

        def fake(method, url, **kwargs):
            calls.append((method, url))
            outcome = queue.pop(0)
            if isinstance(outcome, Exception):
                raise outcome
            return outcome

        monkeypatch.setattr(raw.session, "request", fake)

    install.calls, install.naps = calls, naps
    return install


def test_successful_request_returns_the_payload(raw, transport):
    transport(FakeResponse(payload={"data": []}))
    assert raw.make_api_request("GET", "movies") == {"data": []}
    assert transport.naps == []


def test_empty_body_returns_none(raw, transport):
    transport(FakeResponse(content=b""))
    assert raw.make_api_request("PATCH", "subtitles") is None


def test_rate_limit_is_retried_with_doubling_backoff(raw, transport):
    transport(FakeResponse(429), FakeResponse(429), FakeResponse(payload={"ok": True}))
    assert raw.make_api_request("PATCH", "subtitles", retries=5) == {"ok": True}
    assert transport.naps == [60, 120]


def test_server_error_is_retried(raw, transport):
    transport(FakeResponse(503), FakeResponse(payload={"ok": True}))
    assert raw.make_api_request("GET", "movies", retries=3) == {"ok": True}
    assert transport.naps == [60]


def test_client_error_is_not_retried(raw, transport):
    transport(FakeResponse(404))
    assert raw.make_api_request("GET", "movies", retries=5) is None
    assert transport.naps == []
    assert len(transport.calls) == 1


def test_retries_are_exhausted_then_give_up(raw, transport):
    transport(*[FakeResponse(429)] * 3)
    assert raw.make_api_request("PATCH", "subtitles", retries=2) is None
    assert len(transport.calls) == 3
    assert transport.naps == [60, 120]


def test_timeout_is_retried(raw, transport):
    transport(requests.exceptions.Timeout(), FakeResponse(payload={"ok": True}))
    assert raw.make_api_request("GET", "movies", retries=2) == {"ok": True}
    assert transport.naps == [60]


def test_connection_error_is_retried(raw, transport):
    """Bazarr restarting under a long-lived session is the likeliest failure."""
    transport(
        requests.exceptions.ConnectionError("refused"),
        FakeResponse(payload={"ok": True}),
    )
    assert raw.make_api_request("GET", "movies", retries=3) == {"ok": True}
    assert transport.naps == [60]


def test_retry_after_header_wins_over_backoff(raw, transport):
    """Retrying sooner than a 429 asks only extends the rate-limit window."""
    transport(
        FakeResponse(429, headers={"Retry-After": "17"}),
        FakeResponse(payload={"ok": True}),
    )
    assert raw.make_api_request("PATCH", "subtitles", retries=3) == {"ok": True}
    assert transport.naps == [17]


def test_unparseable_retry_after_falls_back_to_backoff(raw, transport):
    transport(
        FakeResponse(429, headers={"Retry-After": "Wed, 21 Oct 2026 07:28:00 GMT"}),
        FakeResponse(payload={"ok": True}),
    )
    assert raw.make_api_request("PATCH", "subtitles", retries=3) == {"ok": True}
    assert transport.naps == [60]


def test_backoff_doubles_up_to_the_ceiling(raw, monkeypatch):
    """Uncapped, five retries at a 60s start cost 31 minutes for one subtitle."""
    monkeypatch.setattr(raw, "INITIAL_BACKOFF", 60)
    monkeypatch.setattr(raw, "MAX_BACKOFF", 300)
    monkeypatch.setattr(raw.random, "uniform", lambda a, b: 0)
    assert [raw._backoff_delay(n) for n in range(5)] == [60, 120, 240, 300, 300]


def test_backoff_jitter_is_bounded(raw, monkeypatch):
    monkeypatch.setattr(raw, "INITIAL_BACKOFF", 60)
    monkeypatch.setattr(raw, "MAX_BACKOFF", 300)
    monkeypatch.setattr(raw.random, "uniform", lambda a, b: b)
    assert [raw._backoff_delay(n) for n in range(3)] == [90, 150, 270]


def test_negative_retries_still_makes_one_request(raw, monkeypatch):
    """range(retries + 1) was empty for negative retries, so nothing was sent."""
    calls = []

    def refuse(*args, **kwargs):
        calls.append(args)
        raise requests.exceptions.ConnectionError("nope")

    monkeypatch.setattr(raw.session, "request", refuse)
    assert raw.make_api_request("GET", "movies/wanted", retries=-1) is None
    assert len(calls) == 1


def test_failing_item_does_not_abort_the_batch(bat, monkeypatch, sleeps):
    stub_wanted(bat, monkeypatch, {"total": 2, "data": [MOVIE, EPISODE]})
    processed = []

    def flaky(item, media_type):
        processed.append(item)
        if len(processed) == 1:
            raise RuntimeError("Bazarr changed its schema")
        return bat.SATISFIED

    monkeypatch.setattr(bat, "process_subtitles", flaky)
    bat.translate_wanted("movies", {})
    assert processed == [MOVIE, EPISODE]


# Deferral state: stop re-searching providers for hopeless items every day


@pytest.fixture
def state_dir(bat, monkeypatch, tmp_path):
    monkeypatch.setattr(bat, "STATE_DIR", str(tmp_path / "state"))
    return bat


def test_no_source_defers_the_item(state_dir, monkeypatch):
    bat, state = state_dir, {}
    stub_wanted(bat, monkeypatch, {"data": [MOVIE]})
    monkeypatch.setattr(bat, "process_subtitles", lambda i, m: bat.NO_SOURCE)
    bat.translate_wanted("movies", state)
    entry = state["movies:1"]
    assert entry["failures"] == 1
    assert datetime.fromisoformat(entry["next_attempt"]) > datetime.now().astimezone()


def test_repeated_failures_back_off_further(state_dir):
    bat, state = state_dir, {}
    for expected_days, expected_failures in zip(bat.DEFER_DAYS, (1, 2, 3, 4)):
        bat._record_outcome(state, MOVIE, "movies", bat.NO_SOURCE)
        entry = state["movies:1"]
        assert entry["failures"] == expected_failures
        gap = (
            datetime.fromisoformat(entry["next_attempt"]) - datetime.now().astimezone()
        )
        assert abs(gap.total_seconds() - expected_days * 86400) < 60


def test_backoff_stops_growing_at_the_last_step(state_dir):
    bat, state = (
        state_dir,
        {"movies:1": {"failures": 99, "next_attempt": "2020-01-01T00:00:00"}},
    )
    bat._record_outcome(state, MOVIE, "movies", bat.NO_SOURCE)
    gap = (
        datetime.fromisoformat(state["movies:1"]["next_attempt"])
        - datetime.now().astimezone()
    )
    assert abs(gap.total_seconds() - bat.DEFER_DAYS[-1] * 86400) < 60


@pytest.mark.parametrize("outcome_name", ["TRANSLATED", "SATISFIED"])
def test_success_clears_the_deferral(state_dir, outcome_name):
    bat = state_dir
    state = {"movies:1": {"failures": 3, "next_attempt": "2099-01-01T00:00:00"}}
    bat._record_outcome(state, MOVIE, "movies", getattr(bat, outcome_name))
    assert "movies:1" not in state


def test_transient_failure_does_not_defer(state_dir):
    bat, state = state_dir, {}
    bat._record_outcome(state, MOVIE, "movies", bat.FAILED)
    assert state == {}


def test_deferred_items_are_skipped(state_dir, monkeypatch):
    bat = state_dir
    future = (datetime.now().astimezone() + timedelta(days=2)).isoformat(
        timespec="seconds"
    )
    state = {"movies:1": {"failures": 1, "next_attempt": future}}
    stub_wanted(bat, monkeypatch, {"data": [MOVIE]})
    monkeypatch.setattr(
        bat, "process_subtitles", lambda i, m: pytest.fail("processed a deferred item")
    )
    bat.translate_wanted("movies", state)


def test_item_is_retried_once_the_deferral_lapses(state_dir, monkeypatch):
    bat = state_dir
    past = (datetime.now().astimezone() - timedelta(days=1)).isoformat(
        timespec="seconds"
    )
    state = {"movies:1": {"failures": 1, "next_attempt": past}}
    seen = []
    stub_wanted(bat, monkeypatch, {"data": [MOVIE]})
    monkeypatch.setattr(
        bat, "process_subtitles", lambda i, m: seen.append(i) or bat.SATISFIED
    )
    bat.translate_wanted("movies", state)
    assert seen == [MOVIE]


def test_unparseable_entry_does_not_block_the_item(state_dir, monkeypatch):
    bat = state_dir
    state = {"movies:1": {"failures": 1, "next_attempt": "not a date"}}
    seen = []
    stub_wanted(bat, monkeypatch, {"data": [MOVIE]})
    monkeypatch.setattr(
        bat, "process_subtitles", lambda i, m: seen.append(i) or bat.SATISFIED
    )
    bat.translate_wanted("movies", state)
    assert seen == [MOVIE]


def test_state_is_pruned_when_bazarr_stops_wanting_an_item(state_dir, monkeypatch):
    bat = state_dir
    state = {
        "movies:1": {"failures": 1, "next_attempt": "2099-01-01T00:00:00"},
        "movies:999": {"failures": 5, "next_attempt": "2099-01-01T00:00:00"},
        "episodes:7": {"failures": 1, "next_attempt": "2099-01-01T00:00:00"},
    }
    stub_wanted(bat, monkeypatch, {"data": [MOVIE]})
    monkeypatch.setattr(bat, "process_subtitles", lambda i, m: bat.SATISFIED)
    bat.translate_wanted("movies", state)
    assert "movies:999" not in state  # no longer wanted, forgotten
    assert "episodes:7" in state  # other media type left alone


# State file round-trip


def test_state_survives_a_save_and_load(state_dir):
    bat = state_dir
    bat.save_state({"movies:1": {"failures": 2, "next_attempt": "2099-01-01T00:00:00"}})
    assert bat.load_state() == {
        "movies:1": {"failures": 2, "next_attempt": "2099-01-01T00:00:00"}
    }


def test_missing_state_file_is_not_an_error(state_dir):
    assert state_dir.load_state() == {}


def test_corrupt_state_file_is_ignored(state_dir):
    bat = state_dir
    os.makedirs(bat.STATE_DIR, exist_ok=True)
    Path(bat._state_path()).write_text("{not json")
    assert bat.load_state() == {}


def test_unwritable_state_dir_does_not_crash_the_run(bat, monkeypatch, tmp_path):
    """No volume mounted is a supported deployment, it just loses the deferrals."""
    blocker = tmp_path / "blocked"
    blocker.write_text("i am a file, not a directory")
    monkeypatch.setattr(bat, "STATE_DIR", str(blocker / "state"))
    bat.save_state({"movies:1": {"failures": 1}})
    assert bat.load_state() == {}


# Run deadline


def test_run_stops_at_the_deadline(bat, monkeypatch):
    stub_wanted(bat, monkeypatch, {"data": [MOVIE, MOVIE, MOVIE]})
    monkeypatch.setattr(bat, "RUN_DEADLINE", 100)
    # One monotonic() read per item, before it is processed.
    clock = iter([0, 150, 999])
    monkeypatch.setattr(bat.time, "monotonic", lambda: next(clock))
    seen = []
    monkeypatch.setattr(
        bat, "process_subtitles", lambda i, m: seen.append(i) or bat.SATISFIED
    )
    bat.translate_wanted("movies", {}, started=0)
    assert len(seen) == 1  # budget was gone before the second item started


def test_deadline_of_zero_disables_the_budget(bat, monkeypatch):
    stub_wanted(bat, monkeypatch, {"data": [MOVIE, MOVIE]})
    monkeypatch.setattr(bat, "RUN_DEADLINE", 0)
    monkeypatch.setattr(bat.time, "monotonic", lambda: 10**9)
    seen = []
    monkeypatch.setattr(
        bat, "process_subtitles", lambda i, m: seen.append(i) or bat.SATISFIED
    )
    bat.translate_wanted("movies", {}, started=0)
    assert len(seen) == 2


# Configuration parsing: a typo must not take the daemon down


@pytest.mark.parametrize(
    "value,expected",
    [
        pytest.param(None, 120, id="unset"),
        pytest.param("", 120, id="empty"),
        pytest.param("  45 ", 45, id="padded"),
        pytest.param("120s", 120, id="unit-suffix"),
        pytest.param("nope", 120, id="not-a-number"),
        pytest.param("0", 1, id="below-the-minimum"),
        pytest.param("-30", 1, id="negative"),
    ],
)
def test_env_int(raw, monkeypatch, value, expected):
    if value is None:
        monkeypatch.delenv("REQUEST_TIMEOUT", raising=False)
    else:
        monkeypatch.setenv("REQUEST_TIMEOUT", value)
    assert raw._env_int("REQUEST_TIMEOUT", 120, minimum=1) == expected


def test_bad_values_name_the_variable_and_still_import(clean_env, caplog):
    """CI starts the container with no configuration and needs it alive 10s
    later, so a bad value must not be worse than a missing one."""
    clean_env.setenv("REQUEST_TIMEOUT", "120s")
    clean_env.setenv("INITIAL_BACKOFF", "-30")
    with caplog.at_level("WARNING"):
        module = _load_module(clean_env)
    assert module.REQUEST_TIMEOUT == 120
    assert module.INITIAL_BACKOFF == 1
    assert "REQUEST_TIMEOUT" in caplog.text
    assert "INITIAL_BACKOFF" in caplog.text


@pytest.mark.parametrize(
    "value,expected",
    [
        pytest.param("*/10 * * * *", "*/10 * * * *", id="valid"),
        pytest.param("@daily", "@daily", id="shorthand"),
        pytest.param("", "0 6 * * *", id="empty-falls-back"),
        pytest.param("every 10 minutes", "0 6 * * *", id="prose-falls-back"),
        pytest.param("0 25 * * *", "0 6 * * *", id="out-of-range-falls-back"),
        pytest.param("0 6 * *", "0 6 * * *", id="too-few-fields-falls-back"),
    ],
)
def test_cron_schedule_is_validated_at_import(clean_env, value, expected):
    """Regression: croniter() was built inside the loop, so a typo killed the
    process on its first iteration rather than at configuration time."""
    clean_env.setenv("CRON_SCHEDULE", value)
    assert _load_module(clean_env).CRON_SCHEDULE == expected


@pytest.mark.parametrize(
    "value,expected",
    [
        pytest.param(None, "INFO", id="default"),
        pytest.param("debug", "DEBUG", id="case-insensitive"),
        pytest.param("WARNING", "WARNING", id="quieter"),
        pytest.param("chatty", "INFO", id="nonsense-falls-back"),
    ],
)
def test_log_level(clean_env, value, expected):
    if value is not None:
        clean_env.setenv("LOG_LEVEL", value)
    module = _load_module(clean_env)
    assert module.LOG_LEVEL == logging.getLevelNamesMapping()[expected]
    assert module.logger.isEnabledFor(module.LOG_LEVEL)


@pytest.mark.parametrize(
    "scheme,expected",
    [
        pytest.param(None, "http", id="default"),
        pytest.param("https", "https", id="https"),
        pytest.param("HTTPS", "https", id="case-insensitive"),
        pytest.param("ftp", "http", id="unsupported-falls-back"),
    ],
)
def test_bazarr_scheme(clean_env, scheme, expected):
    if scheme is not None:
        clean_env.setenv("BAZARR_SCHEME", scheme)
    assert _load_module(clean_env).BAZARR_SCHEME == expected


def test_https_reaches_the_url(clean_env, monkeypatch):
    clean_env.setenv("BAZARR_SCHEME", "https")
    clean_env.setenv("BAZARR_HOSTNAME", "bazarr.example.com")
    module = _load_module(clean_env)
    seen = []

    def record(method, url, **kwargs):
        seen.append(url)
        raise requests.exceptions.ConnectionError("stop here")

    monkeypatch.setattr(module.session, "request", record)
    module.make_api_request("GET", "movies/wanted")
    assert seen == ["https://bazarr.example.com:6767/api/movies/wanted"]


def test_hostname_given_as_a_url_is_repaired(clean_env):
    """http://https://host is the natural mistake and produces an InvalidURL."""
    clean_env.setenv("BAZARR_HOSTNAME", "https://bazarr.example.com/")
    assert _load_module(clean_env).BAZARR_HOSTNAME == "bazarr.example.com"


# The scheduler, reachable for the first time now that it is a function


def test_run_forever_runs_then_stops(bat, monkeypatch):
    runs, waits = [], []

    def one_run():
        runs.append("ran")
        bat._shutdown.set()

    def next_run(now=None):
        waits.append(1)
        return bat._now()

    monkeypatch.setattr(bat, "get_next_run", next_run)
    monkeypatch.setattr(bat, "main", one_run)
    bat.run_forever()
    assert runs == ["ran"]
    # Exactly one pass: the loop must re-check the flag rather than relying on
    # the next _sleep to bail out.
    assert len(waits) == 1


def test_run_forever_does_not_start_a_run_after_a_stop(bat, monkeypatch):
    """docker stop during the wait must not kick off a six-hour run."""
    monkeypatch.setattr(
        bat, "get_next_run", lambda now=None: bat._now() + timedelta(hours=1)
    )
    monkeypatch.setattr(
        bat, "main", lambda: pytest.fail("started a run while shutting down")
    )
    bat._shutdown.set()
    bat.run_forever()


def test_run_forever_wakes_up_when_asked_to_stop(bat, monkeypatch):
    """Regression: the loop slept through the whole cron gap, so docker stop
    always waited out the grace period and then killed us."""
    monkeypatch.setattr(
        bat, "get_next_run", lambda now=None: bat._now() + timedelta(days=1)
    )
    monkeypatch.setattr(bat, "main", lambda: pytest.fail("ran early"))
    threading.Timer(0.05, bat._shutdown.set).start()
    started = time.monotonic()
    bat.run_forever()
    assert time.monotonic() - started < 5


def test_run_forever_survives_a_failing_run(bat, monkeypatch):
    calls = []

    def boom():
        calls.append(1)
        if len(calls) == 2:
            bat._shutdown.set()
        raise RuntimeError("Bazarr went away")

    monkeypatch.setattr(bat, "get_next_run", lambda now=None: bat._now())
    monkeypatch.setattr(bat, "main", boom)
    bat.run_forever()
    assert len(calls) == 2


def test_sigterm_asks_for_a_shutdown(bat):
    bat._request_shutdown(signal.SIGTERM, None)
    assert bat._shutdown.is_set()


def test_translate_wanted_stops_between_items_on_shutdown(bat, monkeypatch):
    stub_wanted(bat, monkeypatch, {"data": [MOVIE, MOVIE, MOVIE]})
    seen = []

    def process(item, media_type):
        seen.append(item)
        bat._shutdown.set()
        return bat.SATISFIED

    monkeypatch.setattr(bat, "process_subtitles", process)
    bat.translate_wanted("movies", {})
    assert seen == [MOVIE]


@pytest.mark.parametrize(
    "schedule,now,expected",
    [
        pytest.param("0 6 * * *", (2026, 3, 1, 5), (2026, 3, 1, 6), id="later-today"),
        pytest.param(
            "0 6 * * *", (2026, 3, 1, 6), (2026, 3, 2, 6), id="never-returns-now"
        ),
        pytest.param(
            "*/15 * * * *", (2026, 3, 1, 5, 1), (2026, 3, 1, 5, 15), id="step"
        ),
    ],
)
def test_get_next_run_follows_the_schedule(bat, monkeypatch, schedule, now, expected):
    """Aware datetimes, matching what _now() hands the scheduler in production."""
    monkeypatch.setattr(bat, "CRON_SCHEDULE", schedule)
    assert (
        bat.get_next_run(datetime(*now).astimezone())
        == datetime(*expected).astimezone()
    )


def test_main_does_episodes_before_movies(bat, monkeypatch):
    """Whichever runs first gets the RUN_DEADLINE budget, so the order is a
    decision, not an accident of dict ordering."""
    seen = []
    monkeypatch.setattr(bat, "load_state", dict)
    monkeypatch.setattr(bat, "save_state", lambda state: None)
    monkeypatch.setattr(
        bat, "translate_wanted", lambda mt, state, started=None: seen.append(mt)
    )
    bat.main()
    assert seen == ["episodes", "movies"]


def test_main_saves_state_even_when_a_run_raises(bat, monkeypatch):
    saved = []

    def boom(*args, **kwargs):
        raise RuntimeError("Bazarr changed its schema")

    monkeypatch.setattr(bat, "load_state", lambda: {"movies:1": {"failures": 1}})
    monkeypatch.setattr(bat, "save_state", saved.append)
    monkeypatch.setattr(bat, "translate_wanted", boom)
    with pytest.raises(RuntimeError):
        bat.main()
    assert saved == [{"movies:1": {"failures": 1}}]


def test_the_title_comes_from_the_media_type_table(bat, monkeypatch, caplog):
    """The title only ever reaches a log line, so nothing else pins the field."""
    monkeypatch.setattr(bat, "get_subtitles_info", lambda *a, **kw: None)
    with caplog.at_level("INFO"):
        bat.process_subtitles(MOVIE, "movies")
        bat.process_subtitles(EPISODE, "episodes")
    assert "Processing movie: Test Movie" in caplog.text
    assert "Processing episode: Test Series" in caplog.text
