import json
import logging
import os
import random
import signal
import threading
import time
from datetime import datetime, timedelta
from typing import NamedTuple

import requests
from croniter import croniter

DEFAULT_CRON = "0 6 * * *"


def _env_int(name, default, minimum=None):
    """A whole number from the environment, saying which variable was wrong.

    Never fatal. The container is started with no configuration at all in CI,
    and a typo in one tunable should not stop a daemon whose other settings are
    fine: a process that keeps running and says what it ignored is easier to
    fix than one that restarts in a loop.
    """
    raw = os.environ.get(name, "").strip()
    if not raw:
        value = default
    else:
        try:
            value = int(raw)
        except ValueError:
            logger.warning(f"{name}={raw!r} is not a whole number, using {default}")
            value = default
    if minimum is not None and value < minimum:
        logger.warning(
            f"{name}={value} is below the minimum of {minimum}, using {minimum}"
        )
        value = minimum
    return value


def _env_cron(name, default):
    """A cron expression croniter can parse, or the default with a warning.

    Checked here rather than in get_next_run() so a typo cannot kill the
    process on its first loop iteration, hours after it was configured.
    """
    value = os.environ.get(name, "").strip() or default
    if croniter.is_valid(value):
        return value
    logger.warning(f"{name}={value!r} is not a cron expression, using {default!r}")
    return default


def _env_scheme(name, default="http"):
    """The URL scheme to reach Bazarr on."""
    value = os.environ.get(name, "").strip().lower() or default
    if value in ("http", "https"):
        return value
    logger.warning(f"{name}={value!r} is not http or https, using {default!r}")
    return default


def _env_host(name):
    """A bare hostname. A full URL here would build http://https://host."""
    value = os.environ.get(name, "").strip()
    if "://" in value:
        logger.warning(
            f"{name} should be a bare hostname, not a URL - dropping the scheme"
        )
        value = value.split("://", 1)[1].rstrip("/")
    return value


def _env_level(name, default):
    """A logging level, plus the complaint to make once logging is configured."""
    requested = os.environ.get(name, "").strip().upper()
    if not requested:
        return default, None
    level = logging.getLevelNamesMapping().get(requested)
    if level is None:
        return default, (
            f"{name}={requested!r} is not a log level, "
            f"using {logging.getLevelName(default)}"
        )
    return level, None


# Logging first: everything below reports its own problems through it.
LOG_LEVEL, _bad_log_level = _env_level("LOG_LEVEL", logging.INFO)
logging.basicConfig(level=LOG_LEVEL, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)
# basicConfig does nothing once the root logger has a handler, which is the case
# whenever this module is loaded a second time, so set our own level as well.
logger.setLevel(LOG_LEVEL)
if _bad_log_level:
    logger.warning(_bad_log_level)

# Bazarr Information
BAZARR_HOSTNAME = _env_host("BAZARR_HOSTNAME")
BAZARR_PORT = os.environ.get("BAZARR_PORT", "6767")
BAZARR_APIKEY = os.environ.get("BAZARR_APIKEY", "")

# Scheme to reach Bazarr on. The API key travels in a header, so https is worth
# having whenever Bazarr is not on a trusted LAN.
BAZARR_SCHEME = _env_scheme("BAZARR_SCHEME")

CRON_SCHEDULE = _env_cron("CRON_SCHEDULE", DEFAULT_CRON)

FIRST_LANG = os.environ.get("FIRST_LANG", "pl")

# Run immediately once and exit (useful for testing / on-demand runs)
RUN_NOW = os.environ.get("RUN_NOW", "").lower() in ("1", "true", "yes")

# Request timeout in seconds (default: 120s - translations can be slow).
# urllib3 rejects a timeout of 0 with a ValueError that is not a
# RequestException, so it would escape make_api_request entirely.
REQUEST_TIMEOUT = _env_int("REQUEST_TIMEOUT", 120, minimum=1)

# Delay between processing each subtitle in seconds (default: 5s)
# Helps avoid hitting Google Translate rate limits (5 req/s)
TRANSLATE_DELAY = _env_int("TRANSLATE_DELAY", 5, minimum=0)

# Maximum number of retries for failed API requests (default: 5)
MAX_RETRIES = _env_int("MAX_RETRIES", 5, minimum=0)

# Initial backoff delay in seconds before first retry (default: 60s). A negative
# would reach time.sleep() inside the retry handler; 0 would retry a 429
# instantly, which only extends the rate-limit window.
INITIAL_BACKOFF = _env_int("INITIAL_BACKOFF", 60, minimum=1)

# Ceiling for the doubling backoff (default: 300s). Without one, five retries
# at a 60s start add up to 31 minutes of sleeping for a single subtitle.
MAX_BACKOFF = _env_int("MAX_BACKOFF", 300, minimum=1)

# Stop starting new items once a run has been going this long, so a slow run
# cannot swallow the next scheduled one (default: 6h, 0 disables).
RUN_DEADLINE = _env_int("RUN_DEADLINE", 21600, minimum=0)

# Where to remember items nothing could be done for. If it is not writable the
# daemon still works, it just retries hopeless items on every run.
STATE_DIR = os.environ.get("STATE_DIR", "/state")

# How long to leave an item alone after consecutive failures, in days.
DEFER_DAYS = (1, 3, 7, 30)

HEADERS = {"Accept": "application/json", "X-API-KEY": BAZARR_APIKEY}


class MediaType(NamedTuple):
    """What differs between the two halves of the Bazarr API.

    The dict key this is stored under is also the prefix used in the state
    file, so it has to stay plural: renaming it would orphan every deferral
    already recorded on disk.
    """

    noun: str  # what the translate endpoint calls it, and how logs read
    id_field: str  # the item's own id in a wanted-list entry
    title_field: str
    id_param: str  # how the subtitle endpoints want that id
    series_id_field: str | None = None  # episodes are scoped by their series too
    series_param: str | None = None


# Episodes first: that is the order runs have always used, and whichever goes
# first gets the RUN_DEADLINE budget.
MEDIA_TYPES = {
    "episodes": MediaType(
        "episode",
        "sonarrEpisodeId",
        "seriesTitle",
        "episodeid",
        "sonarrSeriesId",
        "seriesid",
    ),
    "movies": MediaType("movie", "radarrId", "title", "radarrid"),
}

# What processing one item accomplished.
TRANSLATED = "translated"  # a translation request went out
SATISFIED = "satisfied"  # nothing to do, the target language is already there
NO_SOURCE = "no_source"  # nothing to translate from, worth deferring
FAILED = "failed"  # the lookup itself failed, just try again next run

# Set when the process has been asked to stop. Everything that sleeps waits on
# this instead, so a stop takes effect in milliseconds rather than at the end of
# a cron interval.
_shutdown = threading.Event()


def _sleep(seconds):
    """Sleep, unless we are shutting down. True means stop what you are doing."""
    return _shutdown.wait(seconds)


def _request_shutdown(signum, _frame):
    logger.info(f"Received {signal.Signals(signum).name}, shutting down")
    _shutdown.set()


session = requests.Session()


def make_api_request(method, endpoint, retries=0, **kwargs):
    """Helper function for making API requests with retry and exponential backoff.

    Args:
        method: HTTP method (GET, PATCH, etc.)
        endpoint: API endpoint path
        retries: Max retry attempts for rate-limit/server errors (0 = no retries)
        **kwargs: Additional arguments passed to requests
    """
    url = f"{BAZARR_SCHEME}://{BAZARR_HOSTNAME}:{BAZARR_PORT}/api/{endpoint}"
    logger.debug(f"Making {method} request to: {url}")

    for attempt in range(max(retries, 0) + 1):
        try:
            response = session.request(
                method, url, headers=HEADERS, timeout=REQUEST_TIMEOUT, **kwargs
            )
            response.raise_for_status()
            logger.debug(f"API Response: {response.status_code}")
            return response.json() if response.content else None
        # ConnectTimeout subclasses both of these, so catching them together
        # keeps the ordering from deciding which handler wins. Bazarr getting
        # restarted underneath a long-lived session lands here.
        except (requests.exceptions.Timeout, requests.exceptions.ConnectionError) as e:
            logger.warning(f"Could not reach {url}: {e}")
            if attempt < retries:
                backoff = _backoff_delay(attempt)
                logger.info(
                    f"Retrying in {backoff}s (attempt {attempt + 1}/{retries})..."
                )
                if _sleep(backoff):
                    return None
            else:
                logger.error(f"Giving up on {url} after {max(retries, 0) + 1} attempts")
                return None
        except requests.exceptions.HTTPError as e:
            status = e.response.status_code if e.response is not None else None
            # Retry on rate-limit (429) and server errors (5xx)
            if status and (status == 429 or status >= 500) and attempt < retries:
                backoff = _retry_after(e.response) or _backoff_delay(attempt)
                logger.warning(
                    f"HTTP {status} from {url}. "
                    f"Retrying in {backoff}s (attempt {attempt + 1}/{retries})..."
                )
                if _sleep(backoff):
                    return None
            else:
                logger.error(f"API request failed: {e}")
                return None
        except requests.exceptions.RequestException as e:
            logger.error(f"API request failed: {e}")
            return None


def _retry_after(response):
    """Seconds the server asked us to wait, or None if it did not say.

    Retrying sooner than a 429 asks for just extends the rate-limit window.
    """
    if response is None:
        return None
    header = response.headers.get("Retry-After")
    try:
        return max(0.0, float(header))
    except (TypeError, ValueError):
        return None  # may be an HTTP-date, which Bazarr does not send


def _backoff_delay(attempt):
    """Calculate exponential backoff with jitter.

    Doubles from INITIAL_BACKOFF up to MAX_BACKOFF, plus 0-30s of jitter.
    Example with a 60s start and a 300s ceiling: 60, 120, 240, 300, 300.
    """
    delay = min(INITIAL_BACKOFF * (2**attempt), MAX_BACKOFF)
    jitter = random.uniform(0, 30)
    return delay + jitter


def _state_path():
    return os.path.join(STATE_DIR, "deferred.json")


def load_state():
    """Items we gave up on previously, keyed by "<media_type>:<id>"."""
    try:
        with open(_state_path()) as handle:
            state = json.load(handle)
        return state if isinstance(state, dict) else {}
    except FileNotFoundError:
        return {}
    except (OSError, ValueError) as e:
        logger.warning(f"Ignoring unreadable state at {_state_path()}: {e}")
        return {}


def save_state(state):
    """Write the deferral state, or explain why hopeless items will repeat."""
    path = _state_path()
    try:
        os.makedirs(STATE_DIR, exist_ok=True)
        # Write and rename so a crash cannot leave a half-written file behind.
        temporary = f"{path}.tmp"
        with open(temporary, "w") as handle:
            json.dump(state, handle, indent=2, sort_keys=True)
        os.replace(temporary, path)
    except OSError as e:
        logger.warning(
            f"Could not save state to {path}: {e}. "
            "Items with no source subtitles will be retried every run."
        )


def _item_id(item, media_type):
    return item.get(MEDIA_TYPES[media_type].id_field)


def _state_key(item, media_type):
    return f"{media_type}:{_item_id(item, media_type)}"


def _now():
    """Aware local time - TZ still decides the schedule and the logged dates."""
    return datetime.now().astimezone()


def _defer_until(failures):
    """When to look at an item again after this many consecutive failures."""
    days = DEFER_DAYS[min(failures, len(DEFER_DAYS)) - 1]
    return _now() + timedelta(days=days)


def _is_deferred(entry, now):
    if not entry:
        return False
    try:
        return datetime.fromisoformat(entry["next_attempt"]) > now
    except (KeyError, TypeError, ValueError):
        # Unparseable entry, treat the item as due. Naive timestamps written before
        # _now() went aware land here too: they lapse once and are rewritten aware.
        return False


def _record_outcome(state, item, media_type, outcome):
    """Back an item off after a hopeless run, or clear it once it resolves."""
    key = _state_key(item, media_type)
    if outcome == NO_SOURCE:
        failures = state.get(key, {}).get("failures", 0) + 1
        next_attempt = _defer_until(failures)
        state[key] = {
            "failures": failures,
            "next_attempt": next_attempt.isoformat(timespec="seconds"),
        }
        logger.info(
            f"Nothing to translate from after {failures} attempts, "
            f"leaving it until {next_attempt:%Y-%m-%d}"
        )
    elif outcome in (TRANSLATED, SATISFIED):
        state.pop(key, None)


def _entries(response):
    """The data list of a Bazarr list response, or [] if it is missing or empty."""
    data = (response or {}).get("data")
    return data if isinstance(data, list) else []


def _find_sub(subs, lang):
    """First subtitle for lang that Bazarr already has on disk, or None."""
    return next((s for s in subs if s.get("code2") == lang and s.get("path")), None)


def get_subtitles_info(media_type, **params):
    """Get subtitle information for episode or movie"""
    return make_api_request("GET", media_type, params=params)


def get_current_subs(media_type, params):
    """Subtitles Bazarr currently holds for one item, or None if the lookup failed.

    Bazarr wants the ids as repeated `key[]` query params.
    """
    info = get_subtitles_info(media_type, **{f"{k}[]": v for k, v in params.items()})
    entries = _entries(info)
    if not entries:
        return None
    return entries[0].get("subtitles") or []


def download_subtitles(media_type, lang, **params):
    """Download subtitles for specified language"""
    endpoint = f"{media_type}/subtitles"
    params.update({"language": lang, "forced": False, "hi": False})
    return make_api_request("PATCH", endpoint, params=params)


def translate_subtitles(sub_path, target_lang, entity_type, media_id):
    """Translate subtitles to the target language, retrying through rate limits.

    entity_type is Bazarr's singular 'movie'/'episode', unlike media_type
    everywhere else in this file, which is the plural endpoint segment.
    """
    params = {
        "action": "translate",
        "language": target_lang,
        "path": sub_path,
        "type": entity_type,
        "id": media_id,
        "forced": False,
        "hi": False,
        "original_format": True,
    }
    return make_api_request("PATCH", "subtitles", retries=MAX_RETRIES, params=params)


def process_subtitles(item, media_type):
    """Process subtitles for a movie or episode.

    Returns one of TRANSLATED, SATISFIED, NO_SOURCE or FAILED.
    """
    spec = MEDIA_TYPES[media_type]
    noun = spec.noun
    item_id = _item_id(item, media_type)
    series_id = item.get(spec.series_id_field) if spec.series_id_field else None
    title = item.get(spec.title_field)

    logger.info(f"Processing {noun}: {title} (ID: {item_id})")

    # requests drops query params whose value is None, so a missing ID would
    # widen the lookup below into an unfiltered query over the whole library
    if not item_id or (spec.series_id_field and not series_id):
        logger.error(f"Skipping {noun} with no usable ID: {title}")
        return FAILED

    params = {spec.id_param: item_id}
    if spec.series_param:
        params[spec.series_param] = series_id
    logger.info(f"Attempting to download {FIRST_LANG} subtitles...")
    result = download_subtitles(media_type, FIRST_LANG, **params)
    logger.info(f"Download {FIRST_LANG} subtitles result: {result}")

    logger.info("Checking current subtitles status...")
    subs = get_current_subs(media_type, params)
    if subs is None:
        logger.error(f"Failed to get media info for {title} (ID: {item_id})")
        return FAILED

    logger.info(f"Found {len(subs)} existing subtitles")
    if logger.isEnabledFor(logging.DEBUG):
        logger.debug(
            "Available subtitles: %s",
            [f"{s.get('code2', 'unknown')}: {s.get('path', 'no path')}" for s in subs],
        )

    if _find_sub(subs, FIRST_LANG):
        logger.info(f"Found existing {FIRST_LANG} subtitles, skipping...")
        return SATISFIED

    logger.info("Looking for English subtitles...")
    en_sub = _find_sub(subs, "en")
    if not en_sub:
        logger.info("No English subtitles found, attempting to download...")
        download_subtitles(media_type, "en", **params)
        en_sub = _find_sub(get_current_subs(media_type, params) or [], "en")
        logger.info(
            f"English subtitles after download: {'found' if en_sub else 'still missing'}"
        )

    if en_sub:
        logger.info(f"Found English subtitles at: {en_sub['path']}")
        logger.info(f"Attempting to translate from English to {FIRST_LANG}...")
        result = translate_subtitles(en_sub["path"], FIRST_LANG, noun, item_id)
        logger.info(f"Translation result: {result}")
        return TRANSLATED

    logger.error("No English subtitles with valid path found or downloaded")
    return NO_SOURCE


def _due_items(items, media_type, state):
    """The wanted items worth attempting now, dropping ones still backed off."""
    now = _now()
    due = [
        item
        for item in items
        if not _is_deferred(state.get(_state_key(item, media_type)), now)
    ]
    if len(due) < len(items):
        logger.info(
            f"Leaving {len(items) - len(due)} {media_type} deferred from earlier runs"
        )
    return due


def _forget_resolved(items, media_type, state):
    """Drop state for items Bazarr no longer wants, so the file cannot grow forever."""
    wanted_keys = {_state_key(item, media_type) for item in items}
    prefix = f"{media_type}:"
    for key in [k for k in state if k.startswith(prefix) and k not in wanted_keys]:
        del state[key]


def translate_wanted(media_type, state, started=None):
    """Download and translate subtitles for every wanted movie or episode."""
    noun = MEDIA_TYPES[media_type].noun
    logger.info(f"Starting {noun} subtitles translation process...")
    wanted = make_api_request(
        "GET", f"{media_type}/wanted", params={"start": 0, "length": -1}
    )
    items = _entries(wanted)
    if not items:
        logger.info(f"No {media_type} found needing subtitles")
        return

    _forget_resolved(items, media_type, state)
    logger.info(f"Found {len(items)} {media_type} needing subtitles")
    due = _due_items(items, media_type, state)

    for i, item in enumerate(due):
        if _shutdown.is_set():
            logger.warning(
                f"Shutting down, {len(due) - i} {media_type} left for the next run"
            )
            return
        if _out_of_time(started):
            logger.warning(
                f"Run budget of {RUN_DEADLINE}s used up, "
                f"{len(due) - i} {media_type} left for the next run"
            )
            return
        try:
            outcome = process_subtitles(item, media_type)
        except Exception:
            logger.exception(f"Failed to process {noun}, skipping to the next item")
            outcome = FAILED
        _record_outcome(state, item, media_type, outcome)
        if outcome == TRANSLATED and TRANSLATE_DELAY and i < len(due) - 1:
            logger.debug("Waiting %ss before next item...", TRANSLATE_DELAY)
            if _sleep(TRANSLATE_DELAY):
                return


def _out_of_time(started):
    return bool(
        RUN_DEADLINE
        and started is not None
        and time.monotonic() - started >= RUN_DEADLINE
    )


def main():
    state = load_state()
    started = time.monotonic()
    try:
        for media_type in MEDIA_TYPES:
            translate_wanted(media_type, state, started)
    finally:
        save_state(state)


def get_next_run(now=None):
    """When the schedule next fires. Takes the clock so tests can pin it."""
    schedule = croniter(CRON_SCHEDULE, now or _now())
    return schedule.get_next(datetime)


def run_forever():
    """Run main() on CRON_SCHEDULE until asked to stop."""
    while not _shutdown.is_set():
        # Read the clock once: reading it again below leaves a window where a
        # backwards step makes the wait negative.
        now = _now()
        next_run = get_next_run(now)
        wait_seconds = max(0.0, (next_run - now).total_seconds())
        logger.info(
            f"Next run scheduled at {next_run:%Y-%m-%d %H:%M:%S}, "
            f"{int(wait_seconds)}s from now"
        )
        if _sleep(wait_seconds):
            break
        logger.info("Starting the translate...")
        try:
            main()
        except Exception:
            logger.exception("Run failed, waiting for the next scheduled run")
    logger.info("Stopped")


if __name__ == "__main__":
    # Warn rather than exit: an unconfigured container that keeps running is
    # easier to inspect than one that restarts in a loop.
    missing = [
        name
        for name, value in (
            ("BAZARR_HOSTNAME", BAZARR_HOSTNAME),
            ("BAZARR_APIKEY", BAZARR_APIKEY),
        )
        if not value
    ]
    if missing:
        logger.warning(f"{' and '.join(missing)} not set - every API call will fail")

    # As PID 1 the kernel drops SIGTERM unless a handler is installed, which is
    # why `docker stop` waited out the whole grace period and then killed us.
    # SIGINT is left alone on purpose: Python's own handler already turns it
    # into KeyboardInterrupt, even at PID 1.
    signal.signal(signal.SIGTERM, _request_shutdown)

    try:
        if RUN_NOW:
            logger.info("RUN_NOW enabled - running immediately")
            main()
            logger.info("Run complete. Exiting.")
        else:
            run_forever()
    except KeyboardInterrupt:
        logger.info("Interrupted")
        raise SystemExit(130)
