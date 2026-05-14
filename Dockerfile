FROM python:3.11-slim

WORKDIR /app

# ffmpeg — для merge видео+аудио. curl/unzip — для установки deno.
RUN apt-get update && \
    apt-get install -y --no-install-recommends ffmpeg curl unzip ca-certificates && \
    rm -rf /var/lib/apt/lists/*

# Deno — JS runtime для yt-dlp. Без него YouTube extractor предупреждает
# "No supported JavaScript runtime" и отдаёт неполные ответы (часть форматов
# и метаданных пропадают, а на видео с возрастным/гео-ограничением падает в ошибку).
RUN curl -fsSL https://deno.land/install.sh | sh -s -- --yes && \
    cp /root/.deno/bin/deno /usr/local/bin/deno && \
    chmod +x /usr/local/bin/deno && \
    deno --version

# Ставим nightly yt-dlp — у YouTube часто ломаются старые extractors
# (видел "Sign in to confirm you're not a bot" на legitимных видео из-за
# устаревшей версии). Nightly обновляется быстрее stable.
RUN pip install --no-cache-dir flask flask-cors && \
    pip install --no-cache-dir --pre "yt-dlp[default]" && \
    yt-dlp --version

COPY server.py .
COPY zoom-downloader.html .
COPY admin.html .

RUN mkdir -p downloads cookies

# Render/Railway сами пробрасывают свой $PORT в контейнер.
EXPOSE 5000

CMD ["python3", "-u", "server.py"]
