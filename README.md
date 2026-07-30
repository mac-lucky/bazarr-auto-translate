<img src="icon.svg" width="96" align="right" alt="">

# bazarr-auto-translate

[![Docker Pulls](https://img.shields.io/docker/pulls/maclucky/bazarr-auto-translate)](https://hub.docker.com/r/maclucky/bazarr-auto-translate)
[![Docker Image Version](https://img.shields.io/docker/v/maclucky/bazarr-auto-translate/latest)](https://hub.docker.com/r/maclucky/bazarr-auto-translate/tags)
[![GitHub Actions Workflow Status](https://github.com/mac-lucky/bazarr-auto-translate/actions/workflows/ci.yml/badge.svg)](https://github.com/mac-lucky/bazarr-auto-translate/actions/workflows/ci.yml)
[![Platform](https://img.shields.io/badge/platform-amd64%20%7C%20arm64-blue)](https://hub.docker.com/r/maclucky/bazarr-auto-translate/tags)

This project automatically downloads and translates subtitles for episodes and movies using the Bazarr API.

## How it Works

1. The script checks for movies and episodes that need subtitles
2. For each item found:
   - Attempts to download subtitles in the target language (FIRST_LANG)
   - If not available, looks for English subtitles
   - If English subtitles are found, translates them to the target language
   - Logs all actions for monitoring

## Requirements

- Docker
- Bazarr API key
- Running Bazarr instance

## Environment Variables

| Variable | Default | What it does |
|---|---|---|
| `BAZARR_HOSTNAME` | (required) | Hostname of your Bazarr instance |
| `BAZARR_PORT` | `6767` | Port of your Bazarr instance |
| `BAZARR_APIKEY` | (required) | Your Bazarr API key |
| `CRON_SCHEDULE` | `0 6 * * *` | When to run, in the container's local time |
| `FIRST_LANG` | `pl` | Target language code |
| `RUN_NOW` | `false` | Run once immediately and exit, instead of scheduling |
| `REQUEST_TIMEOUT` | `120` | Seconds to wait on an API call before giving up |
| `TRANSLATE_DELAY` | `5` | Seconds to pause after each translation, to stay under Google Translate's rate limit. Only applies when a translation actually happened |
| `MAX_RETRIES` | `5` | Retry attempts for a rate-limited or failing translate call |
| `INITIAL_BACKOFF` | `60` | Seconds before the first retry, doubling after that |
| `MAX_BACKOFF` | `300` | Ceiling for the doubling backoff |
| `RUN_DEADLINE` | `21600` | Stop starting new items after this many seconds, so a slow run cannot swallow the next scheduled one. `0` disables |
| `STATE_DIR` | `/state` | Where to remember items nothing could be done for, so they are not re-searched every run. Works without a volume, it just forgets between runs |

### Deferring hopeless items

An item with no subtitles in your target language and no English subtitles to
translate from stays on Bazarr's wanted list indefinitely. Without somewhere to
record that, every run asks the providers again and gets the same answer.

Mount a volume at `STATE_DIR` and the retry gap grows as attempts fail: 1 day,
then 3, 7, and 30. Anything that succeeds, or that Bazarr stops wanting, is
forgotten immediately.

## Running with Docker

### Using Pre-built Image

Pull and run the image directly from Docker Hub:
```sh
docker pull maclucky/bazarr-auto-translate:latest
docker run -e BAZARR_HOSTNAME=your_bazarr_hostname \
           -e BAZARR_PORT=6767 \
           -e BAZARR_APIKEY=your_bazarr_apikey \
           -e CRON_SCHEDULE='0 6 * * *' \
           -e FIRST_LANG=pl \
           -v bazarr-auto-translate-state:/state \
           maclucky/bazarr-auto-translate:latest
```

### Building Locally

1. Clone the repository:
    ```sh
    git clone https://github.com/mac-lucky/bazarr-auto-translate.git
    cd bazarr-auto-translate
    ```

2. Build the Docker image:
    ```sh
    docker build -t bazarr-auto-translate .
    ```

3. Run the container:
```sh
docker run  -e BAZARR_HOSTNAME=your_bazarr_hostname \
            -e BAZARR_PORT=6767 \
            -e BAZARR_APIKEY=your_bazarr_apikey \
            -e CRON_SCHEDULE='0 6 * * *' \
            -e FIRST_LANG=pl \
            -v bazarr-auto-translate-state:/state \
            bazarr-auto-translate
```

## Logging

The script includes detailed logging of all operations, including:
- API requests and responses
- Subtitle download attempts
- Translation processes
- Error messages

Logs can be viewed using:
```sh
docker logs <container_name>
```

