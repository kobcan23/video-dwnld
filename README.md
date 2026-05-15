# video-dwnld

Веб-сервис для скачивания видео с YouTube, Rutube, Zoom-записей, GetCourse и любых других источников, которые поддерживает [yt-dlp](https://github.com/yt-dlp/yt-dlp). Похож на ss-downloader / savefrom, но свой и без рекламы.

🌐 **Развёрнут:** http://186.246.2.141/ (временно на IP, скоро будет `https://infokadr.ru`)

## Что умеет

- 🎥 **YouTube** — через трёхслойный fallback `Piped → Invidious → yt-dlp`, поэтому работает даже когда Google закручивает гайки.
- 🇷🇺 **Rutube, VK, ОК** — через российский IP сервера (хостится в Москве на Timeweb), отсюда обходит гео-блок.
- 🎓 **GetCourse, Zoom, прочее** — стандартный yt-dlp.
- 🍪 **Cookies** — можно один раз загрузить через `/admin` для платных/возрастных видео.
- 📊 **История скачиваний** — кто/что качал, видна в `/admin`.
- 🤖 **PO-токены** — отдельный контейнер `pot-provider` генерирует свежие токены YouTube, что чинит ~80% «вы не робот?»-отказов.

## Архитектура

```
┌─────────┐   :80/:443    ┌────────┐   :5000   ┌────────────┐
│ Browser ├──────────────▶│ Caddy  ├──────────▶│ Flask app  │
└─────────┘  (HTTPS auto) └────────┘           │ + yt-dlp   │
                                               │ + Piped    │
                                               │ + Invidious│
                                               └──────┬─────┘
                                                      │ :4416
                                              ┌───────▼────────┐
                                              │ pot-provider   │
                                              │ (PO-токены)    │
                                              └────────────────┘
```

Три контейнера через `docker-compose`. Caddy сам берёт Let's Encrypt-сертификаты, когда DNS указывает на сервер.

## Структура

| Файл | Что делает |
|------|-----------|
| `server.py` | Flask-сервер, yt-dlp обёртка, fallback-логика |
| `zoom-downloader.html` | Главная страница пользователя |
| `admin.html` | Админка (`/admin`) — куки + история |
| `Dockerfile` | Python 3.11 + ffmpeg + deno (для yt-dlp JS-runtime) + nightly yt-dlp + pot-provider plugin |
| `docker-compose.yml` | Три сервиса: `app`, `pot-provider`, `caddy` |
| `Caddyfile` | Reverse-proxy конфиг |
| `data/state/history.jsonl` | Лог скачиваний (volume-mounted) |
| `data/cookies/` | Глобальный cookies.txt |
| `data/downloads/` | Скачанные видео |

## Запуск

### Локально (для разработки)

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install --pre 'yt-dlp[default]' flask flask-cors bgutil-ytdlp-pot-provider
python server.py  # порт 5000
```

### На сервере через Docker

```bash
git clone git@github.com:kobcan23/video-dwnld.git /opt/video-dwnld
cd /opt/video-dwnld
echo "ADMIN_PASSWORD=$(openssl rand -base64 24 | tr -d '/+=' | head -c 22)" > .env
# Создайте Caddyfile под свой домен (см. ниже)
docker compose up -d
```

### Caddyfile минимальный (только IP, без HTTPS)

```
:80 {
    reverse_proxy app:5000
}
```

### Caddyfile с автоматическим HTTPS

```
example.com, *.example.com {
    reverse_proxy app:5000
}
```

## API

### Публичные endpoints

| Method | Path | Описание |
|--------|------|----------|
| GET | `/` | Главная страница |
| GET | `/healthz` | Проба живости (для мониторинга). 200 = всё ок, 503 = критическая проблема |
| POST | `/api/formats` | Список доступных форматов для URL. Body: `{"url": "..."}` |
| POST | `/api/download` | Старт скачивания. Body: `{"url":"...","format_id":"...","password":"..."}`. Возвращает `task_id`. |
| GET | `/api/status/<task_id>` | Прогресс задачи (poll каждые 1-2 сек) |
| POST | `/api/cancel/<task_id>` | Отмена задачи |
| GET | `/api/file/<file_id>` | Скачать готовый файл |

### Админка (нужен `?password=...`)

| Method | Path | Описание |
|--------|------|----------|
| GET | `/admin` | UI админки |
| POST | `/api/admin/upload-cookies` | Загрузка глобального cookies.txt |
| GET | `/api/admin/cookies-status` | Статус кук (загружены или нет, размер, дата) |
| GET | `/api/admin/history?limit=N` | Последние N записей лога. По умолчанию 200, максимум 1000 |

## Environment

| Переменная | Назначение |
|------------|------------|
| `ADMIN_PASSWORD` | Пароль для `/admin` (обязательно длинный, не `admin123`) |
| `PORT` | Порт Flask (по умолчанию 5000) |
| `POT_PROVIDER_URL` | URL pot-provider сервиса. В docker-compose автоматически `http://pot-provider:4416` |
| `YOUTUBE_COOKIES` | Опционально: текст cookies.txt, который восстанавливается при старте (для эфемерных хостингов как Render) |

## Troubleshooting

### YouTube возвращает «Sign in to confirm you're not a bot»

1. Проверьте `/healthz`. Если `pot_provider: false` — контейнер pot-provider лежит, перезапустите: `docker compose up -d pot-provider`
2. Если pot жив — попробуйте через 5-10 минут, проблема скорее всего в Piped/Invidious инстансах (они тоже под нагрузкой)
3. Если видео 18+ — нужен авторизованный cookies. Залейте через `/admin`

### Rutube возвращает 404 / гео-блок

Хостинг должен быть в РФ. Из-за рубежа Rutube отдаёт страницу-заглушку, которая выглядит как 404 в логах yt-dlp.

### Контейнер app не стартует после ребилда

Проверьте, не сломался ли syntax в `server.py`:
```bash
docker run --rm -v $(pwd)/server.py:/s.py python:3.11-slim python -c "import ast; ast.parse(open('/s.py').read())"
```

## История скачиваний

Хранится в `data/state/history.jsonl` — одна JSON-запись на строку:

```json
{"ts": 1778843335, "task_id": "...", "url": "https://...", "source": "yt-dlp", "status": "done", "title": "...", "files": ["..."], "error": null}
```

Файл переживает обновление контейнера (volume). Можно архивировать ротацией `logrotate`, если разрастётся.

## Стратегия отказоустойчивости YouTube

Трёхслойный fallback:

1. **Piped** (публичные прокси YouTube). Не требует cookies, ходит через свои IP с нормальной репутацией. Список инстансов меняется — храним hardcoded fallback + подгружаем актуальный с piped-instances.kavin.rocks.
2. **Invidious** (другая публичная YouTube-обёртка). `?local=true` проксирует видео через инстанс. Известная особенность: `invidious.f5.si` блокирует `User-Agent: Mozilla/*` через Anubis PoW — ходим с пустым UA.
3. **yt-dlp + PO-токены** + опционально cookies. Прямое скачивание.

Если все три упали — `friendly_error` возвращает понятное сообщение «попробуйте через 5-10 минут».

## Лицензия

Своё. Делайте что хотите, мне всё равно.

## Автор

Игорь Владимирович. Помог Кобик 🦫 (AI-ассистент в OpenClaw).
