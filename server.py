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
COMMON_YDL_OPTS = {
    'quiet': True,
    'no_warnings': True,
    'js_runtimes': {'deno': {'path': 'deno'}},
    'extractor_args': {
        'youtube': {
            'player_client': ['default', 'web', 'mweb', 'tv'],
        },
    },
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
        return ('YouTube требует вход в аккаунт для этого видео '
                '(возрастное/региональное ограничение или бот-защита). '
                'Обновите cookies через /admin — текущие устарели или Google их сбросил.')
    if 'private video' in low:
        return 'Это приватное видео, к нему нет доступа.'
    if 'video unavailable' in low:
        return 'Видео недоступно (удалено или заблокировано в вашем регионе).'
    return s

def get_global_cookies():
    if COOKIES_FILE.exists():
        return str(COOKIES_FILE)
    return None

# ─── Piped (публичные прокси YouTube) ───────────────────────────────────────
# Для YouTube используем Piped как основной источник: он работает через свои
# IP, у которых нормальная репутация у Google, поэтому не требует cookies и
# не блокирует видео с возрастными/региональными ограничениями.
#
# Список Piped-инстансов меняется со временем, поэтому держим хард-кодед
# fallback + пытаемся подгрузить актуальный список с piped-instances.kavin.rocks.

PIPED_INSTANCES_FALLBACK = [
    "https://api.piped.private.coffee",
    "https://pipedapi.kavin.rocks",
    "https://pipedapi.adminforge.de",
    "https://pipedapi.r4fo.com",
    "https://api.piped.yt",
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

def do_download(task_id, url, password, format_id=None):
    task = tasks[task_id]
    task.update({'status':'running','progress':20,'stage':'Получение информации...','log_line':f'Обработка: {url}'})
    out_dir = DOWNLOAD_DIR / task_id
    out_dir.mkdir(exist_ok=True)

    # YouTube ссылки качаем через Piped — стабильнее, не нужны cookies.
    yt_id = extract_youtube_id(url)
    if yt_id:
        try:
            files_paths = piped_download(task_id, yt_id, format_id or '', out_dir)
            files = []
            for f in files_paths:
                if f.is_file():
                    fid = str(uuid.uuid4())
                    tasks[task_id].setdefault('file_map',{})[fid] = str(f)
                    files.append({'id':fid,'name':f.name})
            task.update({'status':'done','progress':100,'stage':'Готово!','files':files,'log_line':f'Скачано: {len(files)} файл(ов)'})
            return
        except Exception as e:
            # Если Piped упал — пробуем yt-dlp как fallback (с куками).
            print(f"[piped] failed, fallback to yt-dlp: {e}")
            task['log_line'] = f"Piped не сработал ({e}); пробую yt-dlp..."

    # Не YouTube или Piped не справился — стандартный yt-dlp путь.
    fmt = format_id if (format_id and not format_id.startswith('piped:')) else 'bestvideo+bestaudio/best'

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
    except Exception as e:
        err = friendly_error(e)
        task.update({'status':'error','error':err,'log_line':f'Ошибка: {err}'})

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

    # YouTube → Piped (без куков, обходит бот-защиту).
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
            print(f"[piped /api/formats] {e}; fallback to yt-dlp")
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
