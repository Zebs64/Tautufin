FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY jellyfin_stats ./jellyfin_stats
COPY data ./data
COPY main.py config.ini.example ./

RUN useradd --create-home appuser \
    && mkdir -p /config \
    && chown -R appuser:appuser /config
USER appuser

VOLUME /config
EXPOSE 8181

CMD ["python", "main.py", "--config", "/config/config.ini"]
