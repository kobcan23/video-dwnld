#!/usr/bin/env python3
import os, uuid, threading
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

tasks = {}

def get_global_cookies():
    if COOKIES_FILE.exists():
        return str(COOKIES_FILE)
    return None

def do_download(task_id, url, password, format_id=None):
    task = tasks[task_id]
    task.update({'status':'running','progress':20,'stage':'Получение информации...','log_line':f'Обработка: {url}'})
    out_dir = DOWNLOAD_DIR / task_id
    out_dir.mkdir(exist_ok=True)

    fmt = format_id if format_id else 'bestvideo+bestaudio/best'

    ydl_opts = {
        'outtmpl': str(out_dir / '%(title)s.%(ext)s'),
        'format': fmt,
        'merge_output_format': 'mp4',
        'quiet': True,
        'no_warnings': True,
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
        task.update({'status':'error','error':str(e),'log_line':f'Ошибка: {e}'})

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

    ydl_opts = {
        'quiet': True,
        'no_warnings': True,
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

            return jsonify({'title': title, 'formats': formats[:15]})
    except Exception as e:
        return jsonify({'error': str(e)}), 500

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
    print("Downloader: http://0.0.0.0:5000")
    app.run(host='0.0.0.0', port=5000, debug=False)
