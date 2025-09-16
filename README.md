# bazarr-auto-translate

[![Docker Pulls](https://img.shields.io/docker/pulls/maclucky/bazarr-auto-translate)](https://hub.docker.com/r/maclucky/bazarr-auto-translate)
[![Docker Image Version](https://img.shields.io/docker/v/maclucky/bazarr-auto-translate/latest)](https://hub.docker.com/r/maclucky/bazarr-auto-translate/tags)
[![GitHub Actions Workflow Status](https://img.shields.io/github/actions/workflow/status/mac-lucky/bazarr-auto-translate/ci.yml)](https://github.com/mac-lucky/bazarr-auto-translate/actions)

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

- `BAZARR_HOSTNAME`: The hostname of your Bazarr instance (required)
- `BAZARR_PORT`: The port of your Bazarr instance (default: `6767`)
- `BAZARR_APIKEY`: Your Bazarr API key (required)
- `CRON_SCHEDULE`: The cron schedule for running the script (default: `0 6 * * *` - runs at 6 AM daily)
- `FIRST_LANG`: The target language code for subtitles (default: `pl`)

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
           maclucky/bazarr-auto-translate:latest
```

### Building Locally

1. Clone the repository:
    ```sh
    git clone https://github.com/yourusername/bazarr-auto-translate.git
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

