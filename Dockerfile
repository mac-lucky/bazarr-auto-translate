FROM python:3.13-slim
WORKDIR /app
COPY bazarr-auto-translate.py .
COPY requirements.txt .
RUN pip install -r requirements.txt
ENV BAZARR_HOSTNAME=localhost \
    BAZARR_PORT=6767 \
    BAZARR_APIKEY=<bazarr-api-key> \
    CRON_SCHEDULE="0 6 * * *" \
    FIRST_LANG=pl

CMD ["python", "-u", "bazarr-auto-translate.py"]