import os
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

HEADERS = {'Accept': 'application/json', 'X-API-KEY': BAZARR_APIKEY}

# Configure logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

# Simple session without retries
session = requests.Session()

def make_api_request(method, endpoint, **kwargs):
    """Helper function for making API requests"""
    url = f"http://{BAZARR_HOSTNAME}:{BAZARR_PORT}/api/{endpoint}"
    logger.debug(f"Making {method} request to: {url}")
    try:
        response = session.request(method, url, headers=HEADERS, **kwargs)
        response.raise_for_status()
        logger.debug(f"API Response: {response.status_code}")
        return response.json() if response.content else None
    except requests.exceptions.RequestException as e:
        logger.error(f"API request failed: {e}")
        return None

def get_subtitles_info(media_type, **params):
    """Get subtitle information for episode or movie"""
    return make_api_request('GET', media_type, params=params)

def download_subtitles(media_type, lang, **params):
    """Download subtitles for specified language"""
    endpoint = f"{media_type}/subtitles"
    params.update({'language': lang, 'forced': False, 'hi': False})
    return make_api_request('PATCH', endpoint, params=params)

def translate_subtitles(sub_path, target_lang, media_type, media_id):
    """Translate subtitles to target language"""
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
    return make_api_request('PATCH', 'subtitles', params=params)

def process_subtitles(item, media_type):
    """Process subtitles for a movie or episode"""
    item_id = item.get('radarrId' if media_type == 'movies' else 'sonarrEpisodeId')
    series_id = item.get('sonarrSeriesId') if media_type == 'episodes' else None
    title = item.get('title' if media_type == 'movies' else 'seriesTitle')
    
    logger.info(f"Processing {media_type[:-1]}: {title} (ID: {item_id})")
    
    # Download FIRST_LANG subtitles
    params = {'radarrid': item_id} if media_type == 'movies' else {'seriesid': series_id, 'episodeid': item_id}
    logger.info(f"Attempting to download {FIRST_LANG} subtitles...")
    result = download_subtitles(media_type, FIRST_LANG, **params)
    logger.info(f"Download {FIRST_LANG} subtitles result: {result}")
    
    # Check subtitles
    logger.info("Checking current subtitles status...")
    media_info = get_subtitles_info(media_type, **{f"{k}[]": v for k, v in params.items()})
    if not media_info or 'data' not in media_info:
        logger.error("Failed to get media info")
        return
        
    subs = media_info['data'][0]['subtitles']
    logger.info(f"Found {len(subs)} existing subtitles")
    logger.debug(f"Available subtitles: {[f'{s.get('code2', 'unknown')}: {s.get('path', 'no path')}' for s in subs]}")
    
    if any(s['code2'] == FIRST_LANG and s.get('path') for s in subs):
        logger.info(f"Found existing {FIRST_LANG} subtitles, skipping...")
        return
        
    # Try to find or download English subtitles
    logger.info("Looking for English subtitles...")
    en_sub = next((s for s in subs if s['code2'] == 'en' and s.get('path')), None)
    if not en_sub:
        logger.info("No English subtitles found, attempting to download...")
        download_subtitles(media_type, 'en', **params)
        media_info = get_subtitles_info(media_type, **{f"{k}[]": v for k, v in params.items()})
        if media_info and 'data' in media_info:
            en_sub = next((s for s in media_info['data'][0]['subtitles'] if s['code2'] == 'en' and s.get('path')), None)
            logger.info("English subtitles download completed")
    
    if en_sub and en_sub.get('path'):
        logger.info(f"Found English subtitles at: {en_sub['path']}")
        logger.info(f"Attempting to translate from English to {FIRST_LANG}...")
        result = translate_subtitles(en_sub['path'], FIRST_LANG,
                                   'movie' if media_type == 'movies' else 'episode',
                                   item_id)
        logger.info(f"Translation result: {result}")
    else:
        logger.error("No English subtitles with valid path found or downloaded")

def translate_movie_subs():
    logger.info("Starting movie subtitles translation process...")
    wanted = make_api_request('GET', 'movies/wanted', params={'start': 0, 'length': -1})
    if wanted and wanted.get('total', 0) > 0:
        logger.info(f"Found {wanted['total']} movies needing subtitles")
        for movie in wanted['data']:
            process_subtitles(movie, 'movies')
    else:
        logger.info("No movies found needing subtitles")

def translate_episode_subs():
    wanted = make_api_request('GET', 'episodes/wanted', params={'start': 0, 'length': -1})
    if wanted and wanted.get('total', 0) > 0:
        for episode in wanted['data']:
            process_subtitles(episode, 'episodes')

def main():
    translate_episode_subs()
    translate_movie_subs()

def get_next_run():
    """Calculate the next run time based on cron schedule."""
    iter = croniter(CRON_SCHEDULE, datetime.now())
    return iter.get_next(datetime)

if __name__ == "__main__":
    # Main loop with cron scheduling
    while True:
        next_run = get_next_run()
        now = datetime.now()
        wait_seconds = (next_run - now).total_seconds()
        print(f'Next run scheduled at {next_run.strftime("%Y-%m-%d %H:%M:%S")}')
        print(f'Waiting for {int(wait_seconds)} seconds...')
        time.sleep(wait_seconds)
        print('Starting the translate...')
        main()