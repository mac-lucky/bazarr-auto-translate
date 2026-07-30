import os
import random
import requests
from croniter import croniter
from datetime import datetime
import time
import logging

#Bazarr Information
BAZARR_HOSTNAME = os.environ.get('BAZARR_HOSTNAME', '')
BAZARR_PORT = os.environ.get('BAZARR_PORT', '6767')
BAZARR_APIKEY = os.environ.get('BAZARR_APIKEY', '')

CRON_SCHEDULE = os.environ.get('CRON_SCHEDULE', '0 6 * * *')

FIRST_LANG = os.environ.get('FIRST_LANG', 'pl')

# Run immediately once and exit (useful for testing / on-demand runs)
RUN_NOW = os.environ.get('RUN_NOW', '').lower() in ('1', 'true', 'yes')

# Request timeout in seconds (default: 120s - translations can be slow)
REQUEST_TIMEOUT = int(os.environ.get('REQUEST_TIMEOUT', '120'))

# Delay between processing each subtitle in seconds (default: 5s)
# Helps avoid hitting Google Translate rate limits (5 req/s)
TRANSLATE_DELAY = max(0, int(os.environ.get('TRANSLATE_DELAY', '5')))

# Maximum number of retries for failed API requests (default: 5)
MAX_RETRIES = max(0, int(os.environ.get('MAX_RETRIES', '5')))

# Initial backoff delay in seconds before first retry (default: 60s)
INITIAL_BACKOFF = int(os.environ.get('INITIAL_BACKOFF', '60'))

HEADERS = {'Accept': 'application/json', 'X-API-KEY': BAZARR_APIKEY}

# Configure logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

session = requests.Session()

def make_api_request(method, endpoint, retries=0, **kwargs):
    """Helper function for making API requests with retry and exponential backoff.

    Args:
        method: HTTP method (GET, PATCH, etc.)
        endpoint: API endpoint path
        retries: Max retry attempts for rate-limit/server errors (0 = no retries)
        **kwargs: Additional arguments passed to requests
    """
    url = f"http://{BAZARR_HOSTNAME}:{BAZARR_PORT}/api/{endpoint}"
    logger.debug(f"Making {method} request to: {url}")

    for attempt in range(max(retries, 0) + 1):
        try:
            response = session.request(
                method, url, headers=HEADERS, timeout=REQUEST_TIMEOUT, **kwargs
            )
            response.raise_for_status()
            logger.debug(f"API Response: {response.status_code}")
            return response.json() if response.content else None
        except requests.exceptions.Timeout:
            logger.warning(f"Request timed out after {REQUEST_TIMEOUT}s: {url}")
            if attempt < retries:
                backoff = _backoff_delay(attempt)
                logger.info(f"Retrying in {backoff}s (attempt {attempt + 1}/{retries})...")
                time.sleep(backoff)
            else:
                logger.error(f"Request timed out after {retries + 1} attempts: {url}")
                return None
        except requests.exceptions.HTTPError as e:
            status = e.response.status_code if e.response is not None else None
            # Retry on rate-limit (429) and server errors (5xx)
            if status and (status == 429 or status >= 500) and attempt < retries:
                backoff = _backoff_delay(attempt)
                logger.warning(
                    f"HTTP {status} from {url}. "
                    f"Retrying in {backoff}s (attempt {attempt + 1}/{retries})..."
                )
                time.sleep(backoff)
            else:
                logger.error(f"API request failed: {e}")
                return None
        except requests.exceptions.RequestException as e:
            logger.error(f"API request failed: {e}")
            return None


def _backoff_delay(attempt):
    """Calculate exponential backoff with jitter.

    Returns delay in seconds: INITIAL_BACKOFF * 2^attempt + random jitter (0-30s).
    Example with 60s initial: 60s, 120s, 240s, 480s, 960s (+ jitter each).
    """
    delay = INITIAL_BACKOFF * (2 ** attempt)
    jitter = random.uniform(0, 30)
    return delay + jitter

def _entries(response):
    """The data list of a Bazarr list response, or [] if it is missing or empty."""
    data = (response or {}).get('data')
    return data if isinstance(data, list) else []

def _find_sub(subs, lang):
    """First subtitle for lang that Bazarr already has on disk, or None."""
    return next((s for s in subs if s.get('code2') == lang and s.get('path')), None)

def get_subtitles_info(media_type, **params):
    """Get subtitle information for episode or movie"""
    return make_api_request('GET', media_type, params=params)

def get_current_subs(media_type, params):
    """Subtitles Bazarr currently holds for one item, or None if the lookup failed.

    Bazarr wants the ids as repeated `key[]` query params.
    """
    info = get_subtitles_info(media_type, **{f"{k}[]": v for k, v in params.items()})
    entries = _entries(info)
    if not entries:
        return None
    return entries[0].get('subtitles') or []

def download_subtitles(media_type, lang, **params):
    """Download subtitles for specified language"""
    endpoint = f"{media_type}/subtitles"
    params.update({'language': lang, 'forced': False, 'hi': False})
    return make_api_request('PATCH', endpoint, params=params)

def translate_subtitles(sub_path, target_lang, media_type, media_id):
    """Translate subtitles to target language (with retries for rate limits)"""
    params = {
        'action': 'translate',
        'language': target_lang,
        'path': sub_path,
        'type': media_type,
        'id': media_id,
        'forced': False,
        'hi': False,
        'original_format': True
    }
    return make_api_request('PATCH', 'subtitles', retries=MAX_RETRIES, params=params)

def process_subtitles(item, media_type):
    """Process subtitles for a movie or episode.

    Returns True if a translation request was sent, False otherwise.
    """
    noun = media_type[:-1]
    item_id = item.get('radarrId' if media_type == 'movies' else 'sonarrEpisodeId')
    series_id = item.get('sonarrSeriesId') if media_type == 'episodes' else None
    title = item.get('title' if media_type == 'movies' else 'seriesTitle')

    logger.info(f"Processing {noun}: {title} (ID: {item_id})")

    # requests drops query params whose value is None, so a missing ID would
    # widen the lookup below into an unfiltered query over the whole library
    if not item_id or (media_type == 'episodes' and not series_id):
        logger.error(f"Skipping {noun} with no usable ID: {title}")
        return False

    params = {'radarrid': item_id} if media_type == 'movies' else {'seriesid': series_id, 'episodeid': item_id}
    logger.info(f"Attempting to download {FIRST_LANG} subtitles...")
    result = download_subtitles(media_type, FIRST_LANG, **params)
    logger.info(f"Download {FIRST_LANG} subtitles result: {result}")

    logger.info("Checking current subtitles status...")
    subs = get_current_subs(media_type, params)
    if subs is None:
        logger.error(f"Failed to get media info for {title} (ID: {item_id})")
        return False

    logger.info(f"Found {len(subs)} existing subtitles")
    if logger.isEnabledFor(logging.DEBUG):
        logger.debug("Available subtitles: %s",
                     [f"{s.get('code2', 'unknown')}: {s.get('path', 'no path')}" for s in subs])

    if _find_sub(subs, FIRST_LANG):
        logger.info(f"Found existing {FIRST_LANG} subtitles, skipping...")
        return False

    logger.info("Looking for English subtitles...")
    en_sub = _find_sub(subs, 'en')
    if not en_sub:
        logger.info("No English subtitles found, attempting to download...")
        download_subtitles(media_type, 'en', **params)
        en_sub = _find_sub(get_current_subs(media_type, params) or [], 'en')
        logger.info(f"English subtitles after download: {'found' if en_sub else 'still missing'}")

    if en_sub:
        logger.info(f"Found English subtitles at: {en_sub['path']}")
        logger.info(f"Attempting to translate from English to {FIRST_LANG}...")
        result = translate_subtitles(en_sub['path'], FIRST_LANG, noun, item_id)
        logger.info(f"Translation result: {result}")
        return True

    logger.error("No English subtitles with valid path found or downloaded")
    return False

def translate_wanted(media_type):
    """Download and translate subtitles for every wanted movie or episode."""
    noun = media_type[:-1]
    logger.info(f"Starting {noun} subtitles translation process...")
    wanted = make_api_request('GET', f"{media_type}/wanted", params={'start': 0, 'length': -1})
    items = _entries(wanted)
    if not items:
        logger.info(f"No {media_type} found needing subtitles")
        return

    logger.info(f"Found {len(items)} {media_type} needing subtitles")
    for i, item in enumerate(items):
        try:
            translated = process_subtitles(item, media_type)
        except Exception:
            logger.exception(f"Failed to process {noun}, skipping to the next item")
            translated = False
        if translated and TRANSLATE_DELAY and i < len(items) - 1:
            logger.debug(f"Waiting {TRANSLATE_DELAY}s before next item...")
            time.sleep(TRANSLATE_DELAY)

def main():
    translate_wanted('episodes')
    translate_wanted('movies')

def get_next_run():
    """Calculate the next run time based on cron schedule."""
    iter = croniter(CRON_SCHEDULE, datetime.now())
    return iter.get_next(datetime)

if __name__ == "__main__":
    if RUN_NOW:
        logger.info("RUN_NOW enabled - running immediately")
        main()
        logger.info("Run complete. Exiting.")
    else:
        # Main loop with cron scheduling
        while True:
            next_run = get_next_run()
            now = datetime.now()
            wait_seconds = (next_run - now).total_seconds()
            print(f'Next run scheduled at {next_run.strftime("%Y-%m-%d %H:%M:%S")}')
            print(f'Waiting for {int(wait_seconds)} seconds...')
            time.sleep(wait_seconds)
            print('Starting the translate...')
            try:
                main()
            except Exception:
                logger.exception('Run failed, waiting for the next scheduled run')
