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
tasks = {}

def do_download(task_id, url, password, cookies_file=None):
    task = tasks[task_id]
    task.update({'status':'running','progress':20,'stage':'Получение информации...','log_line':f'Обработка: {url}'})
    out_dir = DOWNLOAD_DIR / task_id
    out_dir.mkdir(exist_ok=True)
    ydl_opts = {
        'outtmpl': str(out_dir / '%(title)s.%(ext)s'),
        'format': 'bestvideo+bestaudio/best',
        'merge_output_format': 'mp4',
        'quiet': True,
        'no_warnings': True,
        'progress_hooks': [lambda d: progress_hook(task_id, d)],
    }
    if password:
        ydl_opts['videopassword'] = password
    if cookies_file and Path(cookies_file).exists():
        ydl_opts['cookiefile'] = cookies_file
        task['log_line'] = 'Используются куки для авторизации'
    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=False)
            task['log_line'] = f'Найдено: {info.get("title","Без названия")}'
            task.update({'progress':30,'stage':'Скачивание...'})
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
    finally:
        if cookies_file and Path(cookies_file).exists():
            try: os.remove(cookies_file)
            except: pass

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

@app.route('/api/download', methods=['POST'])
def start_download():
    url = (request.form.get('url') or '').strip()
    password = (request.form.get('password') or '').strip()
    cookies_file = None
    if not url:
        return jsonify({'error':'URL обязателен'}), 400
    if 'cookies' in request.files:
        f = request.files['cookies']
        if f.filename:
            cookies_path = str(COOKIES_DIR / f'{uuid.uuid4()}.txt')
            f.save(cookies_path)
            cookies_file = cookies_path
    task_id = str(uuid.uuid4())
    tasks[task_id] = {'status':'pending','progress':10,'stage':'Запуск...','file_map':{}}
    threading.Thread(target=do_download, args=(task_id, url, password, cookies_file), daemon=True).start()
    return jsonify({'task_id':task_id})

@app.route('/api/status/<task_id>')
def get_status(task_id):
    task = tasks.get(task_id)
    if not task: return jsonify({'error':'Не найдено'}), 404
    return jsonify({'status':task.get('status'),'progress':task.get('progress',0),'stage':task.get('stage',''),'log_line':task.get('log_line',''),'error':task.get('error',''),'files':task.get('files',[])})

@app.route('/api/file/<file_id>')
def serve_file(file_id):
    for task in tasks.values():
        fm = task.get('file_map',{})
        if file_id in fm: return send_file(fm[file_id], as_attachment=True)
    return jsonify({'error':'Не найден'}), 404

if __name__ == '__main__':
    print("Downloader: http://0.0.0.0:5000")
    app.run(host='0.0.0.0', port=5000, debug=False)
