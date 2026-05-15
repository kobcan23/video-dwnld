#!/usr/bin/env python3
import os, uuid, threading, re, json, subprocess, shutil
import urllib.request, urllib.error
from pathlib import Path
from flask import Flask, request, jsonify, send_file
from flask_cors import CORS
import yt_dlp

app = Flask(__name__)
CORS(app)

DOWNLOAD_DIR = Path(__file__).parent / "downloads"
DOWNLOAD_DIR.mkdir(exist_ok=True)
HISTORY_FILE = Path(__file__).parent / "data" / "history.jsonl"
HISTORY_FILE.parent.mkdir(exist_ok=True)
HISTORY_LOCK = threading.Lock()
COOKIES_DIR = Path(__file__).parent / "cookies"
COOKIES_DIR.mkdir(exist_ok=True)

ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "admin123")
COOKIES_FILE = COOKIES_DIR / "global_cookies.txt"

# При старте: если задана переменная окружения YOUTUBE_COOKIES — пишем её
# содержимое в файл с куками. Это решает проблему "куки теряются при
# перезапуске контейнера" на бесплатных хостингах (Render, Railway), где
# нет постоянной файловой системы. Куки переживают рестарты, пока
# переменная задана.
_env_cookies = os.environ.get("YOUTUBE_COOKIES", "").strip()
if _env_cookies:
    try:
        # Поддержка \n как разделителя строк (если задали одной строкой)
        content = _env_cookies.replace("\\n", "\n")
        COOKIES_FILE.write_text(content, encoding="utf-8")
        print(f"[init] cookies восстановлены из YOUTUBE_COOKIES ({len(content)} bytes)")
    except Exception as e:
        print(f"[init] не удалось записать куки из env: {e}")

tasks = {}

# Общие опции yt-dlp, помогающие обходить "Sign in to confirm you're not a bot":
#   — deno как JS runtime (иначе часть экстракторов отключена)
#   — различные player clients (web,mweb,tv выживают в разных ситуациях)
#   — user-agent обычного браузера
#   — PO-токены через bgutil-pot-provider (если POT_PROVIDER_URL задан)
_pot_url = os.environ.get('POT_PROVIDER_URL', '').strip()
_youtube_extractor_args = {
    'player_client': ['default', 'web', 'mweb', 'tv'],
}
_extractor_args = {'youtube': _youtube_extractor_args}
if _pot_url:
    # bgutil-ytdlp-pot-provider читает base_url из extractor-args
    # ключа youtubepot-bgutilhttp.
    _extractor_args['youtubepot-bgutilhttp'] = {'base_url': [_pot_url]}

COMMON_YDL_OPTS = {
    'quiet': True,
    'no_warnings': True,
    'js_runtimes': {'deno': {'path': 'deno'}},
    'extractor_args': _extractor_args,
    'http_headers': {
        'User-Agent': 'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
                      '(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36',
    },
}

# Сообщение об ошибке yt-dlp приводим к виду, понятному пользователю.
def friendly_error(msg: str) -> str:
    s = str(msg)
    low = s.lower()
    if 'sign in to confirm' in low or 'login_required' in low:
        return ('YouTube сейчас не отдаёт это видео анонимно (защита от ботов '
                'или возрастное/региональное ограничение). Попробуйте через 5–10 '
                'минут — мы автоматически перебираем несколько публичных прокси.')
    if 'private video' in low:
        return 'Это приватное видео, к нему нет доступа.'
    if 'video unavailable' in low:
        return 'Видео недоступно (удалено или заблокировано в вашем регионе).'
    return s

def get_global_cookies():
    if COOKIES_FILE.exists():
        return str(COOKIES_FILE)
    return None

# ─── История скачиваний ──────────────────────────────────────
# Пишем каждое завершённое скачивание в JSONL-файл. Объём на один
import time as _time

def _log_history(task_id, url, source, status, title=None, files=None, error=None, format_id=None):
    """Дописываем запись в history.jsonl. Безопасно к ошибкам — история не должна ронять скачивание."""
    try:
        rec = {
            'ts': int(_time.time()),
            'task_id': task_id,
            'url': url,
            'source': source,  # piped|invidious|yt-dlp
            'status': status,  # done|error
            'title': title,
            'format_id': format_id,
            'files': [f.get('name') for f in (files or []) if f.get('name')],
            'error': error,
        }
        with HISTORY_LOCK:
            with HISTORY_FILE.open('a', encoding='utf-8') as fh:
                fh.write(json.dumps(rec, ensure_ascii=False) + '\n')
    except Exception as e:
        print(f"[history] failed to log: {e}")

def _read_history(limit=200):
    """Читаем последние N записей (от новых к старым). Простой tail — для файла до ~50 MB хватит."""
    if not HISTORY_FILE.exists():
        return []
    try:
        with HISTORY_LOCK:
            with HISTORY_FILE.open('r', encoding='utf-8') as fh:
                lines = fh.readlines()
        out = []
        for line in reversed(lines[-limit:]):
            try:
                out.append(json.loads(line))
            except Exception:
                continue
        return out
    except Exception as e:
        print(f"[history] failed to read: {e}")
        return []

# ─── Piped (публичные прокси YouTube) ───────────────────────────────────────
# Для YouTube используем Piped как основной источник: он работает через свои
# IP, у которых нормальная репутация у Google, поэтому не требует cookies и
# не блокирует видео с возрастными/региональными ограничениями.
#
# Список Piped-инстансов меняется со временем, поэтому держим хард-кодед
# fallback + пытаемся подгрузить актуальный список с piped-instances.kavin.rocks.

# Список регулярно обновляем (см. https://github.com/TeamPiped/Piped/wiki/Instances).
# Проверка 2026-05-15: живы api.piped.private.coffee и api.piped.projectsegfau.lt,
# остальные либо лежат, либо отвечают редиректом на сайт.
PIPED_INSTANCES_FALLBACK = [
    "https://api.piped.private.coffee",
    "https://api.piped.projectsegfau.lt",
    "https://pipedapi.kavin.rocks",
    "https://pipedapi.adminforge.de",
    "https://pipedapi.r4fo.com",
    "https://api.piped.yt",
    "https://pipedapi.leptons.xyz",
    "https://pipedapi.smnz.de",
    "https://pipedapi.ducks.party",
    "https://pipedapi.drgns.space",
    "https://pipedapi.darkness.services",
]

_piped_instances_cache = {'list': None, 'fetched_at': 0}

def _piped_instances():
    """Возвращает список Piped API URL. Динамически обновляет раз в час."""
    import time
    now = time.time()
    if _piped_instances_cache['list'] and now - _piped_instances_cache['fetched_at'] < 3600:
        return _piped_instances_cache['list']
    instances = []
    try:
        req = urllib.request.Request(
            "https://piped-instances.kavin.rocks/",
            headers={'User-Agent': 'video-dwnld/1.0'},
        )
        with urllib.request.urlopen(req, timeout=5) as resp:
            data = json.loads(resp.read().decode('utf-8'))
            # Берём те, у которых uptime >90% за сутки, и cors_proxy НЕ нужен —
            # отдают url прямо в videoStreams.
            for i in data:
                if i.get('uptime_24h', 0) >= 80 and i.get('api_url'):
                    instances.append(i['api_url'].rstrip('/'))
    except Exception as e:
        print(f"[piped] не удалось получить список инстансов: {e}")
    # Объединяем с fallback, убираем дубли, сохраняя порядок.
    seen = set()
    merged = []
    for u in instances + PIPED_INSTANCES_FALLBACK:
        if u not in seen:
            seen.add(u)
            merged.append(u)
    _piped_instances_cache['list'] = merged
    _piped_instances_cache['fetched_at'] = now
    return merged

_YT_RE = re.compile(
    r'(?:youtube\.com/(?:watch\?(?:[^#]*&)?v=|shorts/|embed/|live/)|youtu\.be/)([A-Za-z0-9_-]{11})'
)

def extract_youtube_id(url: str):
    """Возвращает 11-символьный YouTube video_id или None."""
    if not url:
        return None
    m = _YT_RE.search(url)
    return m.group(1) if m else None

def piped_get_streams(video_id: str):
    """Запрашивает /streams/<id> у каждого Piped-инстанса, возвращает первый успешный JSON.

    Бросает RuntimeError, если все инстансы упали.
    """
    last_err = None
    for base in _piped_instances():
        try:
            req = urllib.request.Request(
                f"{base}/streams/{video_id}",
                headers={'User-Agent': 'video-dwnld/1.0'},
            )
            with urllib.request.urlopen(req, timeout=8) as resp:
                if resp.status != 200:
                    last_err = f"{base} → HTTP {resp.status}"
                    continue
                data = json.loads(resp.read().decode('utf-8'))
                if data.get('error'):
                    last_err = f"{base} → {data['error']}"
                    continue
                # сохраняем, через какой instance получили
                data['_piped_instance'] = base
                return data
        except Exception as e:
            last_err = f"{base} → {e}"
            continue
    raise RuntimeError(f"Все Piped-инстансы недоступны. Последняя ошибка: {last_err}")

def piped_formats_list(streams: dict):
    """Превращает ответ Piped в наш формат списка форматов (как у yt-dlp)."""
    formats = []
    seen = set()
    vs = streams.get('videoStreams') or []
    aus = streams.get('audioStreams') or []

    # 1) Progressive (видео+аудио в одном файле) — обычно только 360p/720p, mp4
    for s in vs:
        if s.get('videoOnly', True):
            continue
        h = s.get('width', 0) and int(s.get('quality', '0p').rstrip('p') or 0)
        ext = (s.get('format') or 'mp4').lower().replace('mpeg_4', 'mp4')
        key = ('prog', h, ext)
        if key in seen:
            continue
        seen.add(key)
        size = s.get('contentLength')
        size_str = f" ~{round(int(size)/1024/1024)}MB" if size else ""
        formats.append({
            'id': f"piped:prog:{h}:{ext}",
            'label': f"{h or '?'}p {ext.upper()}{size_str}",
            'height': h or 0,
            'ext': ext,
        })

    # 2) DASH (видео отдельно, аудио отдельно) — собираем merged варианты
    for s in vs:
        if not s.get('videoOnly', False):
            continue
        try:
            h = int(str(s.get('quality', '0p')).rstrip('p') or 0)
        except ValueError:
            continue
        ext_raw = (s.get('format') or '').lower()
        # У Piped формат у video stream бывает MPEG_4 → mp4, либо WEBM
        ext = 'mp4' if 'mp4' in ext_raw or 'mpeg_4' in ext_raw else 'webm'
        # Все варианты сливаем в mp4, чтобы стабильно играли на iOS/Android
        key = ('dash', h, 'mp4')
        if key in seen:
            continue
        seen.add(key)
        vsize = int(s.get('contentLength') or 0)
        # к видео прибавим лучший аудио для оценки размера
        abest = max((int(a.get('contentLength') or 0) for a in aus), default=0)
        total = vsize + abest
        size_str = f" ~{round(total/1024/1024)}MB" if total else ""
        formats.append({
            'id': f"piped:dash:{h}:mp4",
            'label': f"{h}p MP4{size_str}",
            'height': h,
            'ext': 'mp4',
        })

    # 3) Аудио-only (лучший по битрейту)
    if aus:
        abest = max(aus, key=lambda a: a.get('bitrate', 0) or 0)
        bsize = int(abest.get('contentLength') or 0)
        size_str = f" ~{round(bsize/1024/1024)}MB" if bsize else ""
        formats.append({
            'id': "piped:audio:m4a",
            'label': f"Только аудио M4A{size_str}",
            'height': 0,
            'ext': 'm4a',
        })

    # Сортируем по высоте (по убыванию), аудио — в конец.
    formats.sort(key=lambda x: (x['height'] == 0, -x['height']))
    return formats

def piped_download(task_id: str, video_id: str, format_id: str, out_dir: Path):
    """Скачивает видео через Piped с прогрессом. Поддерживает форматы:
    'piped:prog:<h>:<ext>', 'piped:dash:<h>:mp4', 'piped:audio:m4a'.
    Если format_id пустой — берём лучший по умолчанию (≤1080p).
    """
    task = tasks[task_id]
    task.update({'progress': 25, 'stage': 'Получение информации (Piped)...', 'log_line': f'Piped: {video_id}'})
    streams = piped_get_streams(video_id)
    title = streams.get('title') or video_id
    task['title'] = title
    safe = re.sub(r'[^A-Za-z0-9А-Яа-яЁё._\- ]', '_', title)[:120].strip() or video_id
    task['log_line'] = f'Найдено: {title}'

    vs = streams.get('videoStreams') or []
    aus = streams.get('audioStreams') or []

    # Определяем «план» скачивания
    plan = None
    if not format_id:
        # По умолчанию — лучший mp4 до 1080p (DASH), потому что progressive
        # обычно только 360p/720p и не у всех видео есть.
        plan = ('dash', 1080, 'mp4')
    elif format_id.startswith('piped:'):
        parts = format_id.split(':')
        kind = parts[1]
        if kind == 'audio':
            plan = ('audio', 0, 'm4a')
        elif kind == 'prog':
            plan = ('prog', int(parts[2]), parts[3])
        else:  # dash
            plan = ('dash', int(parts[2]), 'mp4')
    else:
        # Не наш id — fallback на лучшее
        plan = ('dash', 1080, 'mp4')

    kind, want_h, ext = plan

    if kind == 'audio':
        abest = max(aus, key=lambda a: a.get('bitrate', 0) or 0)
        out = out_dir / f"{safe}.m4a"
        _piped_fetch(task, abest['url'], out, label='аудио')
        return [out]

    if kind == 'prog':
        cand = [s for s in vs if not s.get('videoOnly', True)]
        # Ищем близкий по высоте формат с тем же ext
        match = None
        for s in cand:
            try:
                h = int(str(s.get('quality', '0p')).rstrip('p') or 0)
            except ValueError:
                continue
            sext = (s.get('format') or 'mp4').lower().replace('mpeg_4', 'mp4')
            if h == want_h and sext == ext:
                match = s
                break
        if not match and cand:
            match = cand[0]
        if not match:
            raise RuntimeError('Нет progressive-формата для этого видео.')
        out = out_dir / f"{safe}.{ext}"
        _piped_fetch(task, match['url'], out, label=f"{want_h}p")
        return [out]

    # kind == 'dash' — качаем видео и аудио раздельно, мёржим ffmpeg
    only_v = [s for s in vs if s.get('videoOnly', False)]
    # Сортируем по высоте по убыванию и берём первый, у которого height <= want_h
    def _h(s):
        try:
            return int(str(s.get('quality', '0p')).rstrip('p') or 0)
        except ValueError:
            return 0
    only_v.sort(key=_h, reverse=True)
    vmatch = next((s for s in only_v if _h(s) <= want_h), only_v[-1] if only_v else None)
    if not vmatch:
        raise RuntimeError('Нет видеопотока для этого ролика.')
    amatch = max(aus, key=lambda a: a.get('bitrate', 0) or 0) if aus else None
    if not amatch:
        raise RuntimeError('Нет аудиопотока для этого ролика.')

    tmp_v = out_dir / "_video.tmp"
    tmp_a = out_dir / "_audio.tmp"
    out = out_dir / f"{safe}.mp4"

    task.update({'progress': 35, 'stage': f"Скачивание видео {_h(vmatch)}p...", 'log_line': f"video stream {_h(vmatch)}p"})
    _piped_fetch(task, vmatch['url'], tmp_v, label=f"видео {_h(vmatch)}p", progress_base=35, progress_span=40)

    task.update({'progress': 75, 'stage': 'Скачивание аудио...', 'log_line': 'audio stream'})
    _piped_fetch(task, amatch['url'], tmp_a, label="аудио", progress_base=75, progress_span=15)

    task.update({'progress': 92, 'stage': 'Сборка файла...', 'log_line': 'ffmpeg merge'})
    # Сливаем без перекодирования
    proc = subprocess.run(
        ['ffmpeg', '-y', '-i', str(tmp_v), '-i', str(tmp_a),
         '-c', 'copy', '-movflags', '+faststart', str(out)],
        capture_output=True, text=True
    )
    if proc.returncode != 0:
        # Если copy не прошёл (несовместимые контейнеры) — попробуем перекодировать аудио в AAC
        proc = subprocess.run(
            ['ffmpeg', '-y', '-i', str(tmp_v), '-i', str(tmp_a),
             '-c:v', 'copy', '-c:a', 'aac', '-movflags', '+faststart', str(out)],
            capture_output=True, text=True
        )
        if proc.returncode != 0:
            raise RuntimeError(f"ffmpeg merge failed: {proc.stderr[-400:]}")

    try:
        tmp_v.unlink(missing_ok=True)
        tmp_a.unlink(missing_ok=True)
    except Exception:
        pass

    return [out]

def _piped_fetch(task: dict, url: str, dest: Path, label: str = '',
                 progress_base: int = 30, progress_span: int = 50):
    """Скачивает url в dest с обновлением прогресса в task."""
    req = urllib.request.Request(url, headers={'User-Agent': 'video-dwnld/1.0'})
    with urllib.request.urlopen(req, timeout=30) as resp:
        total = int(resp.headers.get('Content-Length') or 0)
        got = 0
        with open(dest, 'wb') as f:
            while True:
                if task.get('cancelled'):
                    raise Exception('Отменено пользователем')
                chunk = resp.read(64 * 1024)
                if not chunk:
                    break
                f.write(chunk)
                got += len(chunk)
                if total:
                    pct = got / total
                    task['progress'] = int(progress_base + pct * progress_span)
                    task['log_line'] = f"{label}: {got//1024//1024} MB / {total//1024//1024} MB"
                else:
                    task['log_line'] = f"{label}: {got//1024//1024} MB"

# ─── /Piped ─────────────────────────────────────────────────────────────────

# ─── Invidious (второй слой fallback) ───────────────────────────────────────
# Если все Piped-инстансы легли — пробуем Invidious. Он тоже проксирует YouTube
# без куков, используя флаг ?local=true (отдаёт URL'ы вида https://<instance>/videoplayback?...).
# Некоторые инстансы (f5.si) блокируют Mozilla-UA через Anubis PoW — ходим с пустым UA.

INVIDIOUS_INSTANCES_FALLBACK = [
    "https://invidious.f5.si",
    "https://invidious.projectsegfau.lt",
    "https://invidious.nerdvpn.de",
    "https://yewtu.be",
    "https://invidious.private.coffee",
    "https://invidious.materialio.us",
    "https://inv.tux.pizza",
    "https://invidious.adminforge.de",
]

_invidious_instances_cache = {'list': None, 'fetched_at': 0}

def _invidious_instances():
    """Список Invidious API URL. Динамически обновляем раз в час через api.invidious.io."""
    import time
    now = time.time()
    if _invidious_instances_cache['list'] and now - _invidious_instances_cache['fetched_at'] < 3600:
        return _invidious_instances_cache['list']
    instances = []
    try:
        req = urllib.request.Request(
            "https://api.invidious.io/instances.json?sort_by=health",
            headers={'User-Agent': 'video-dwnld/1.0'},
        )
        with urllib.request.urlopen(req, timeout=5) as resp:
            data = json.loads(resp.read().decode('utf-8'))
            for host, info in data:
                if info.get('api') and info.get('type') == 'https' and info.get('uri'):
                    instances.append(info['uri'].rstrip('/'))
    except Exception as e:
        print(f"[invidious] не удалось получить список инстансов: {e}")
    seen = set()
    merged = []
    for u in instances + INVIDIOUS_INSTANCES_FALLBACK:
        if u not in seen:
            seen.add(u)
            merged.append(u)
    _invidious_instances_cache['list'] = merged
    _invidious_instances_cache['fetched_at'] = now
    return merged

def invidious_get_video(video_id: str):
    """Опросить Invidious-инстансы, вернуть первый успешный JSON с полями title/formats.

    Все URL в ответе будут проксированы через этот же инстанс (?local=true).
    """
    last_err = None
    for base in _invidious_instances():
        try:
            req = urllib.request.Request(
                f"{base}/api/v1/videos/{video_id}?fields=title,adaptiveFormats,formatStreams&local=true",
                headers={'User-Agent': ''},  # пустой UA обходит Anubis на f5.si
            )
            with urllib.request.urlopen(req, timeout=8) as resp:
                if resp.status != 200:
                    last_err = f"{base} → HTTP {resp.status}"
                    continue
                raw = resp.read()
                if not raw or raw[:1] != b'{':
                    last_err = f"{base} → ответ не JSON ({len(raw)} bytes)"
                    continue
                data = json.loads(raw.decode('utf-8'))
                if data.get('error') or not (data.get('adaptiveFormats') or data.get('formatStreams')):
                    last_err = f"{base} → {data.get('error', 'no formats')}"
                    continue
                data['_invidious_instance'] = base
                return data
        except Exception as e:
            last_err = f"{base} → {e}"
            continue
    raise RuntimeError(f"Все Invidious-инстансы недоступны. Последняя ошибка: {last_err}")

def invidious_formats_list(video: dict):
    """Превращает ответ Invidious в наш формат списка форматов."""
    formats = []
    seen_dash = set()
    seen_prog = set()
    # adaptive (DASH — видео и аудио раздельно)
    for f in video.get('adaptiveFormats') or []:
        t = (f.get('type') or '').lower()
        if 'video/mp4' not in t:
            continue
        ql = f.get('qualityLabel') or ''
        try:
            h = int(ql.rstrip('p')) if ql else 0
        except ValueError:
            h = 0
        if not h or h in seen_dash:
            continue
        seen_dash.add(h)
        size = f.get('clen') or f.get('contentLength')
        try:
            size_mb = round(int(size) / 1024 / 1024) if size else None
        except Exception:
            size_mb = None
        formats.append({
            'id': f'inv:dash:{h}:mp4',
            'label': f"{h}p MP4" + (f" ~{size_mb}MB" if size_mb else ''),
            'height': h,
            'ext': 'mp4',
        })
    # progressive (видео+аудио вместе — обычно только 360p)
    for f in video.get('formatStreams') or []:
        ql = f.get('qualityLabel') or ''
        try:
            h = int(ql.rstrip('p')) if ql else 0
        except ValueError:
            h = 0
        ext = (f.get('container') or 'mp4').lower()
        key = (h, ext)
        if key in seen_prog:
            continue
        seen_prog.add(key)
        formats.append({
            'id': f'inv:prog:{h}:{ext}',
            'label': f"{h}p {ext.upper()} (без сборки)",
            'height': h,
            'ext': ext,
        })
    # audio only — лучший m4a
    audio_best_br = 0
    for f in video.get('adaptiveFormats') or []:
        t = (f.get('type') or '').lower()
        if 'audio/mp4' not in t:
            continue
        try:
            br = int(f.get('bitrate') or 0)
        except Exception:
            br = 0
        if br > audio_best_br:
            audio_best_br = br
    if audio_best_br:
        formats.append({
            'id': 'inv:audio:m4a',
            'label': 'Только аудио M4A',
            'height': 0,
            'ext': 'm4a',
        })
    formats.sort(key=lambda x: (x['height'] == 0, -x['height']))
    return formats

def invidious_download(task_id: str, video_id: str, format_id: str, out_dir: Path):
    """Скачивает видео через Invidious. Форматы: 'inv:prog:<h>:<ext>', 'inv:dash:<h>:mp4', 'inv:audio:m4a'.
    Пустой format_id → лучший mp4 ≤1080p (DASH).
    """
    task = tasks[task_id]
    task.update({'progress': 25, 'stage': 'Получение информации (Invidious)...', 'log_line': f'Invidious: {video_id}'})
    video = invidious_get_video(video_id)
    title = video.get('title') or video_id
    task['title'] = title
    safe = re.sub(r'[^A-Za-z0-9А-Яа-яЁё._\- ]', '_', title)[:120].strip() or video_id
    task['log_line'] = f"Найдено (Invidious): {title}"

    adaptive = video.get('adaptiveFormats') or []
    progressive = video.get('formatStreams') or []

    if not format_id:
        plan = ('dash', 1080, 'mp4')
    elif format_id.startswith('inv:'):
        parts = format_id.split(':')
        kind = parts[1]
        if kind == 'audio':
            plan = ('audio', 0, 'm4a')
        elif kind == 'prog':
            plan = ('prog', int(parts[2]), parts[3])
        else:
            plan = ('dash', int(parts[2]), 'mp4')
    else:
        plan = ('dash', 1080, 'mp4')

    kind, want_h, ext = plan

    def _h(f):
        try:
            return int(str(f.get('qualityLabel', '0p')).rstrip('p') or 0)
        except ValueError:
            return 0

    if kind == 'audio':
        a_audio = [f for f in adaptive if 'audio/mp4' in (f.get('type') or '').lower()]
        if not a_audio:
            raise RuntimeError('Нет аудиопотока (Invidious).')
        a_best = max(a_audio, key=lambda f: f.get('bitrate', 0) or 0)
        out = out_dir / f"{safe}.m4a"
        _invidious_fetch(task, a_best['url'], out, label='аудио')
        return [out]

    if kind == 'prog':
        cand = [f for f in progressive if (f.get('container') or 'mp4').lower() == ext]
        cand.sort(key=_h, reverse=True)
        match = next((f for f in cand if _h(f) <= want_h), cand[0] if cand else None)
        if not match:
            raise RuntimeError('Нет progressive-формата (Invidious).')
        out = out_dir / f"{safe}.{ext}"
        _invidious_fetch(task, match['url'], out, label=f"{want_h}p")
        return [out]

    # DASH
    only_v = [f for f in adaptive if 'video/mp4' in (f.get('type') or '').lower()]
    only_v.sort(key=_h, reverse=True)
    vmatch = next((f for f in only_v if _h(f) <= want_h), only_v[-1] if only_v else None)
    if not vmatch:
        raise RuntimeError('Нет видеопотока (Invidious).')
    only_a = [f for f in adaptive if 'audio/mp4' in (f.get('type') or '').lower()]
    if not only_a:
        raise RuntimeError('Нет аудиопотока (Invidious).')
    amatch = max(only_a, key=lambda f: f.get('bitrate', 0) or 0)

    tmp_v = out_dir / "_video.tmp"
    tmp_a = out_dir / "_audio.tmp"
    out = out_dir / f"{safe}.mp4"

    task.update({'progress': 35, 'stage': f"Скачивание видео {_h(vmatch)}p (Invidious)...", 'log_line': f"video stream {_h(vmatch)}p"})
    _invidious_fetch(task, vmatch['url'], tmp_v, label=f"видео {_h(vmatch)}p", progress_base=35, progress_span=40)

    task.update({'progress': 75, 'stage': 'Скачивание аудио (Invidious)...', 'log_line': 'audio stream'})
    _invidious_fetch(task, amatch['url'], tmp_a, label="аудио", progress_base=75, progress_span=15)

    task.update({'progress': 92, 'stage': 'Сборка файла...', 'log_line': 'ffmpeg merge'})
    proc = subprocess.run(
        ['ffmpeg', '-y', '-i', str(tmp_v), '-i', str(tmp_a),
         '-c', 'copy', '-movflags', '+faststart', str(out)],
        capture_output=True, text=True
    )
    if proc.returncode != 0:
        proc = subprocess.run(
            ['ffmpeg', '-y', '-i', str(tmp_v), '-i', str(tmp_a),
             '-c:v', 'copy', '-c:a', 'aac', '-movflags', '+faststart', str(out)],
            capture_output=True, text=True
        )
        if proc.returncode != 0:
            raise RuntimeError(f"ffmpeg merge failed: {proc.stderr[-400:]}")

    try:
        tmp_v.unlink(missing_ok=True)
        tmp_a.unlink(missing_ok=True)
    except Exception:
        pass

    return [out]

def _invidious_fetch(task: dict, url: str, dest: Path, label: str = '',
                     progress_base: int = 30, progress_span: int = 50):
    """Скачивает url (возможно относительный) в dest с прогрессом. Пустой UA (Anubis bypass)."""
    if url.startswith('/'):
        cached = _invidious_instances_cache.get('list') or INVIDIOUS_INSTANCES_FALLBACK
        url = cached[0] + url
    req = urllib.request.Request(url, headers={'User-Agent': ''})
    with urllib.request.urlopen(req, timeout=30) as resp:
        total = int(resp.headers.get('Content-Length') or 0)
        got = 0
        with open(dest, 'wb') as f:
            while True:
                if task.get('cancelled'):
                    raise Exception('Отменено пользователем')
                chunk = resp.read(64 * 1024)
                if not chunk:
                    break
                f.write(chunk)
                got += len(chunk)
                if total:
                    pct = got / total
                    task['progress'] = int(progress_base + pct * progress_span)
                    task['log_line'] = f"{label}: {got//1024//1024} MB / {total//1024//1024} MB"
                else:
                    task['log_line'] = f"{label}: {got//1024//1024} MB"

# ─── /Invidious ─────────────────────────────────────────────────────────────

def do_download(task_id, url, password, format_id=None):
    task = tasks[task_id]
    task.update({'status':'running','progress':20,'stage':'Получение информации...','log_line':f'Обработка: {url}'})
    out_dir = DOWNLOAD_DIR / task_id
    out_dir.mkdir(exist_ok=True)

    # YouTube ссылки: Piped → Invidious → yt-dlp (трёхслойный fallback).
    yt_id = extract_youtube_id(url)
    if yt_id:
        # 1) Piped
        try:
            files_paths = piped_download(task_id, yt_id, format_id or '', out_dir)
            files = []
            for f in files_paths:
                if f.is_file():
                    fid = str(uuid.uuid4())
                    tasks[task_id].setdefault('file_map',{})[fid] = str(f)
                    files.append({'id':fid,'name':f.name})
            task.update({'status':'done','progress':100,'stage':'Готово!','files':files,'log_line':f'Скачано: {len(files)} файл(ов) через Piped'})
            _log_history(task_id, url, 'piped', 'done', title=task.get('title'), files=files, format_id=format_id)
            return
        except Exception as e:
            print(f"[piped] failed, fallback to invidious: {e}")
            task['log_line'] = f"Piped не сработал ({e}); пробую Invidious..."
            # Подчистим частичные файлы от Piped
            for stale in out_dir.glob('*'):
                try: stale.unlink()
                except Exception: pass

        # 2) Invidious. Если выбран формат с префиксом piped: — перемапим на inv:
        inv_format = format_id or ''
        if inv_format.startswith('piped:'):
            inv_format = 'inv:' + inv_format[len('piped:'):]
        try:
            files_paths = invidious_download(task_id, yt_id, inv_format, out_dir)
            files = []
            for f in files_paths:
                if f.is_file():
                    fid = str(uuid.uuid4())
                    tasks[task_id].setdefault('file_map',{})[fid] = str(f)
                    files.append({'id':fid,'name':f.name})
            task.update({'status':'done','progress':100,'stage':'Готово!','files':files,'log_line':f'Скачано: {len(files)} файл(ов) через Invidious'})
            _log_history(task_id, url, 'invidious', 'done', title=task.get('title'), files=files, format_id=format_id)
            return
        except Exception as e:
            print(f"[invidious] failed, fallback to yt-dlp: {e}")
            task['log_line'] = f"Invidious тоже не сработал ({e}); пробую yt-dlp..."
            for stale in out_dir.glob('*'):
                try: stale.unlink()
                except Exception: pass

    # Не YouTube или Piped не справился — стандартный yt-dlp путь.
    fmt = format_id if (format_id and not format_id.startswith(('piped:', 'inv:'))) else 'bestvideo+bestaudio/best'

    ydl_opts = {
        **COMMON_YDL_OPTS,
        'outtmpl': str(out_dir / '%(title)s.%(ext)s'),
        'format': fmt,
        'merge_output_format': 'mp4',
        'progress_hooks': [lambda d: progress_hook(task_id, d)],
    }
    if password:
        ydl_opts['videopassword'] = password
    cookies = get_global_cookies()
    if cookies:
        ydl_opts['cookiefile'] = cookies
    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=False)
            task['title'] = info.get("title", "Без названия")
            task['log_line'] = f'Найдено: {info.get("title","Без названия")}'
            task.update({'progress':30,'stage':'Скачивание...'})
            if task.get('cancelled'):
                raise Exception('Отменено пользователем')
            ydl.download([url])
        files = []
        for f in out_dir.iterdir():
            if f.is_file():
                fid = str(uuid.uuid4())
                tasks[task_id].setdefault('file_map',{})[fid] = str(f)
                files.append({'id':fid,'name':f.name})
        task.update({'status':'done','progress':100,'stage':'Готово!','files':files,'log_line':f'Скачано: {len(files)} файл(ов)'})
        _log_history(task_id, url, 'yt-dlp', 'done', title=task.get('title'), files=files, format_id=format_id)
    except Exception as e:
        err = friendly_error(e)
        task.update({'status':'error','error':err,'log_line':f'Ошибка: {err}'})
        _log_history(task_id, url, 'yt-dlp', 'error', title=task.get('title'), error=err, format_id=format_id)

def progress_hook(task_id, d):
    task = tasks.get(task_id)
    if not task: return
    if d['status'] == 'downloading':
        try:
            pct = float(d.get('_percent_str','0').strip().replace('%',''))
            task.update({'progress':int(pct*0.6+30),'stage':f"Скачивание: {d.get('_percent_str','').strip()}",'log_line':f"{d.get('_percent_str','').strip()} | {d.get('_speed_str','?')}"})
        except: pass
    elif d['status'] == 'finished':
        task.update({'progress':90,'stage':'Обработка...'})

@app.route('/')
def index():
    return send_file(Path(__file__).parent / 'zoom-downloader.html')

@app.route('/admin')
def admin():
    return send_file(Path(__file__).parent / 'admin.html')

@app.route('/api/formats', methods=['POST'])
def get_formats():
    url = (request.json.get('url') or '').strip()
    if not url:
        return jsonify({'error': 'URL обязателен'}), 400

    # YouTube → Piped → Invidious → yt-dlp (без куков, обходит бот-защиту).
    yt_id = extract_youtube_id(url)
    if yt_id:
        try:
            streams = piped_get_streams(yt_id)
            return jsonify({
                'title': streams.get('title') or 'YouTube видео',
                'formats': piped_formats_list(streams)[:15],
                'source': 'piped',
            })
        except Exception as e:
            print(f"[piped /api/formats] {e}; fallback to invidious")
        try:
            video = invidious_get_video(yt_id)
            return jsonify({
                'title': video.get('title') or 'YouTube видео',
                'formats': invidious_formats_list(video)[:15],
                'source': 'invidious',
            })
        except Exception as e:
            print(f"[invidious /api/formats] {e}; fallback to yt-dlp")
            # дальше — yt-dlp как fallback

    ydl_opts = {
        **COMMON_YDL_OPTS,
        'skip_download': True,
    }
    cookies = get_global_cookies()
    if cookies:
        ydl_opts['cookiefile'] = cookies

    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=False)
            title = info.get('title', 'Без названия')
            formats_raw = info.get('formats', [])

            formats = []
            seen = set()

            for f in formats_raw:
                vcodec = f.get('vcodec', 'none')
                acodec = f.get('acodec', 'none')
                ext = f.get('ext', '')
                fid = f.get('format_id', '')
                height = f.get('height')
                filesize = f.get('filesize') or f.get('filesize_approx')
                if not fid:
                    continue
                has_video = vcodec and vcodec != 'none'
                has_audio = acodec and acodec != 'none'
                if not has_video and not has_audio:
                    continue
                if has_video and has_audio:
                    type_label = ''
                elif has_video:
                    type_label = ' (видео)'
                else:
                    type_label = ' (аудио)'
                res = f"{height}p" if height else ext.upper()
                size_str = f" ~{round(filesize/1024/1024)}MB" if filesize else ""
                key = f"{height}-{ext}-{type_label}"
                if key in seen:
                    continue
                seen.add(key)
                formats.append({
                    'id': fid,
                    'label': f"{res} {ext.upper()}{type_label}{size_str}",
                    'height': height or 0,
                    'ext': ext,
                })

            # Сортируем по качеству
            formats.sort(key=lambda x: x['height'], reverse=True)

            return jsonify({'title': title, 'formats': formats[:15], 'source': 'yt-dlp'})
    except Exception as e:
        return jsonify({'error': friendly_error(e)}), 500

@app.route('/api/admin/upload-cookies', methods=['POST'])
def upload_cookies():
    password = request.form.get('password', '')
    if password != ADMIN_PASSWORD:
        return jsonify({'error': 'Неверный пароль'}), 403
    if 'cookies' not in request.files:
        return jsonify({'error': 'Файл не найден'}), 400
    f = request.files['cookies']
    if not f.filename:
        return jsonify({'error': 'Файл пустой'}), 400
    f.save(str(COOKIES_FILE))
    return jsonify({'success': True, 'message': 'Куки загружены!'})

@app.route('/api/admin/cookies-status')
def cookies_status():
    password = request.args.get('password', '')
    if password != ADMIN_PASSWORD:
        return jsonify({'error': 'Неверный пароль'}), 403
    if COOKIES_FILE.exists():
        size = COOKIES_FILE.stat().st_size
        mtime = COOKIES_FILE.stat().st_mtime
        import datetime
        dt = datetime.datetime.fromtimestamp(mtime).strftime('%d.%m.%Y %H:%M')
        return jsonify({'exists': True, 'size': size, 'updated': dt})
    return jsonify({'exists': False})

@app.route('/api/admin/history')
def admin_history():
    password = request.args.get('password', '')
    if password != ADMIN_PASSWORD:
        return jsonify({'error': 'Неверный пароль'}), 403
    try:
        limit = int(request.args.get('limit', '200'))
    except ValueError:
        limit = 200
    limit = max(1, min(limit, 1000))
    return jsonify({'items': _read_history(limit=limit)})

@app.route('/api/download', methods=['POST'])
def start_download():
    if request.is_json:
        url = (request.json.get('url') or '').strip()
        password = (request.json.get('password') or '').strip()
        format_id = (request.json.get('format_id') or '').strip()
    else:
        url = (request.form.get('url') or '').strip()
        password = (request.form.get('password') or '').strip()
        format_id = (request.form.get('format_id') or '').strip()
    if not url:
        return jsonify({'error':'URL обязателен'}), 400
    task_id = str(uuid.uuid4())
    tasks[task_id] = {'status':'pending','progress':10,'stage':'Запуск...','file_map':{}}
    threading.Thread(target=do_download, args=(task_id, url, password, format_id or None), daemon=True).start()
    return jsonify({'task_id':task_id})

@app.route('/api/status/<task_id>')
def get_status(task_id):
    task = tasks.get(task_id)
    if not task: return jsonify({'error':'Не найдено'}), 404
    return jsonify({'status':task.get('status'),'progress':task.get('progress',0),'stage':task.get('stage',''),'log_line':task.get('log_line',''),'error':task.get('error',''),'files':task.get('files',[])})

@app.route('/api/cancel/<task_id>', methods=['POST'])
def cancel_task(task_id):
    task = tasks.get(task_id)
    if not task:
        return jsonify({'error': 'Не найдено'}), 404
    task['cancelled'] = True
    task['status'] = 'error'
    task['error'] = 'Отменено пользователем'
    return jsonify({'success': True})

@app.route('/api/file/<file_id>')
def serve_file(file_id):
    for task in tasks.values():
        fm = task.get('file_map',{})
        if file_id in fm: return send_file(fm[file_id], as_attachment=True)
    return jsonify({'error':'Не найден'}), 404

if __name__ == '__main__':
    # Railway/Heroku/Render передают порт через переменную окружения PORT.
    # Локально (без PORT) слушаем 5000 — чтобы можно было запускать `python server.py`.
    port = int(os.environ.get('PORT', 5000))
    print(f"Downloader: http://0.0.0.0:{port}")
    app.run(host='0.0.0.0', port=port, debug=False)
