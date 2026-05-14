FROM python:3.11-slim

WORKDIR /app

RUN apt-get update && apt-get install -y ffmpeg --no-install-recommends && rm -rf /var/lib/apt/lists/*

RUN pip install flask flask-cors yt-dlp --no-cache-dir

COPY server.py .
COPY zoom-downloader.html .

RUN mkdir -p downloads cookies

EXPOSE 5000

CMD ["python3", "server.py"]
