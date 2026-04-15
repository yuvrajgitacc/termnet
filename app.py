import eventlet
eventlet.monkey_patch()

import sqlite3
import os
import uuid
import math
from flask import Flask, render_template, request
from flask_socketio import SocketIO, emit, join_room, leave_room

app = Flask(__name__)
app.config['SECRET_KEY'] = 'termnet_turbo_v3'
app.config['MAX_CONTENT_LENGTH'] = 100 * 1024 * 1024

# Increased buffer and timeout for binary jumbo-streams
socketio = SocketIO(
    app, 
    cors_allowed_origins="*", 
    async_mode='eventlet', 
    ping_timeout=120,
    ping_interval=25,
    max_http_buffer_size=110 * 1024 * 1024 # 110MB for overhead
)

DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'termnet.db')

def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    return conn

def init_db():
    conn = get_db()
    c = conn.cursor()
    c.execute('CREATE TABLE IF NOT EXISTS users (username TEXT PRIMARY KEY)')
    c.execute('CREATE TABLE IF NOT EXISTS rooms (name TEXT PRIMARY KEY, password TEXT)')
    # Using BLOB for high-speed binary storage
    c.execute('''CREATE TABLE IF NOT EXISTS files 
                 (id INTEGER PRIMARY KEY AUTOINCREMENT, 
                  filename TEXT, content BLOB, sender TEXT, room TEXT,
                  filesize INTEGER DEFAULT 0,
                  uploaded_at DATETIME DEFAULT CURRENT_TIMESTAMP)''')
    c.execute("INSERT OR IGNORE INTO rooms VALUES ('lobby', '123')")
    conn.commit()
    conn.close()

init_db()

sessions = {}
active_transfers = {}

@app.route('/')
def index():
    return render_template('index.html')

@socketio.on('connect')
def handle_connect():
    sessions[request.sid] = {'user': 'guest', 'room': 'home'}

@socketio.on('restore_session')
def handle_restore(data):
    sid = request.sid
    user, room = data.get('user', 'guest'), data.get('room', 'home')
    sessions[sid] = {'user': user, 'room': room}
    if room != 'home': join_room(room)

@socketio.on('disconnect')
def handle_disconnect():
    sid = request.sid
    if sid in sessions: del sessions[sid]

@socketio.on('command')
def handle_command(data):
    sid = request.sid
    if sid not in sessions: sessions[sid] = {'user': 'guest', 'room': 'home'}
    raw = data.get('command', '').strip()
    if not raw: return
    parts = raw.split(); cmd = parts[0].lower(); args = parts[1:]
    conn = get_db(); c = conn.cursor()

    try:
        c.execute("DELETE FROM files WHERE uploaded_at <= datetime('now', '-1 day')")
        conn.commit()

        if cmd == "help":
            msg = "\n[ TERMNET TURBO COMMANDS ]\n" + "-"*35 + \
                  "\n  signup/login <user>  : Identity\n  list                 : Show Rooms" + \
                  "\n  create/jump <r>-<p>  : Room Management\n  share/vault          : File Transfer" + \
                  "\n  files                : List Files\n  fetch <filename>     : Download" + \
                  "\n  profile              : View Identity\n  exit / clear         : Navigation\n" + "-"*35
            emit('response', {'type': 'system', 'msg': msg})

        elif cmd == "signup":
            if not args: emit('response', {'type': 'error', 'msg': "Usage: signup <name>"})
            else:
                u = args[0].lower()
                try:
                    c.execute("INSERT INTO users VALUES (?)", (u,))
                    conn.commit(); sessions[sid]['user'] = u
                    emit('response', {'type': 'system', 'msg': f"Identity: {u}", 'user': u})
                except: emit('response', {'type': 'error', 'msg': "Taken."})

        elif cmd == "login":
            u = args[0].lower() if args else ""
            c.execute("SELECT username FROM users WHERE username=?", (u,))
            if c.fetchone():
                sessions[sid]['user'] = u
                emit('response', {'type': 'system', 'msg': f"Auth: {u}", 'user': u})
            else: emit('response', {'type': 'error', 'msg': "No user."})

        elif cmd == "list":
            c.execute("SELECT name FROM rooms")
            rs = "\n".join([f"  ▸ {r['name']}" for r in c.fetchall()])
            emit('response', {'type': 'system', 'msg': f"\n┌─ ROOMS ──┐\n{rs}\n└──────────┘"})

        elif cmd == "jump":
            if len(args) < 3: emit('response', {'type': 'error', 'msg': "jump <n> - <p>"})
            else:
                n, p = args[0].lower(), args[2]
                c.execute("SELECT password FROM rooms WHERE name=?", (n,))
                row = c.fetchone()
                if row and row['password'] == p:
                    old = sessions[sid]['room']
                    if old != 'home': leave_room(old)
                    join_room(n); sessions[sid]['room'] = n
                    emit('response', {'type': 'system', 'msg': f"Connected: {n}", 'room': n})
                else: emit('response', {'type': 'error', 'msg': "Denied."})

        elif cmd == "files":
            r = sessions[sid]['room']
            c.execute("SELECT filename, filesize, sender FROM files WHERE room=? ORDER BY id DESC", (r,))
            fs = "\n".join([f"  ▸ {f['filename']} ({format_size(f['filesize'])}) [{f['sender']}]" for f in c.fetchall()])
            emit('response', {'type': 'system', 'msg': f"\n┌─ '{r}' FILES ──┐\n{fs}\n└────────────────┘" if fs else "[!] Empty."})

        elif cmd == "fetch":
            fn = " ".join(args).strip()
            u, r = sessions[sid]['user'], sessions[sid]['room']
            c.execute("SELECT filename, content, filesize FROM files WHERE LOWER(filename)=LOWER(?) AND (room=? OR (room='private' AND sender=?)) ORDER BY id DESC LIMIT 1", (fn, r, u))
            row = c.fetchone()
            if row:
                content = row['content']
                chunk_size = 4 * 1024 * 1024 # 4MB Download chunks
                total = math.ceil(len(content) / chunk_size)
                emit('file_download_start', {'filename': row['filename'], 'totalChunks': total})
                for i in range(total):
                    emit('file_download_chunk', {'filename': row['filename'], 'chunk': content[i*chunk_size:(i+1)*chunk_size], 'index': i})
                    eventlet.sleep(0)
                emit('file_download_complete', {'filename': row['filename']})
            else: emit('response', {'type': 'error', 'msg': "Not found."})

        elif cmd == "share":
            if sessions[sid]['room'] == 'home': emit('response', {'type': 'error', 'msg': "Jump first."})
            else: emit('trigger_upload', {'mode': 'public'})

        elif cmd == "vault":
            if sessions[sid]['user'] == 'guest': emit('response', {'type': 'error', 'msg': "Login first."})
            else: emit('trigger_upload', {'mode': 'private'})

        elif cmd == "exit":
            r = sessions[sid]['room']
            if r != 'home':
                leave_room(r); sessions[sid]['room'] = 'home'
                emit('response', {'type': 'system', 'msg': "At home.", 'room': 'home'})
        
        elif cmd == "msg":
            r = sessions[sid]['room']
            if r != 'home': emit('chat_msg', {'user': sessions[sid]['user'], 'msg': " ".join(args)}, to=r)

    except Exception as e: print(f"Error: {e}")
    finally: conn.close()

@socketio.on('file_upload_start')
def handle_upload_start(d):
    tid = str(uuid.uuid4())[:8]
    active_transfers[tid] = {'chunks': {}, 'total': d['totalChunks'], 'received': 0, 'filename': d['filename'], 'filesize': d['filesize'], 'mode': d['mode'], 'sid': request.sid}
    emit('upload_ready', {'transferId': tid})

@socketio.on('file_upload_chunk')
def handle_upload_chunk(d):
    tid = d.get('transferId')
    if tid in active_transfers:
        active_transfers[tid]['chunks'][d['index']] = d['chunk'] # Binary data received
        active_transfers[tid]['received'] += 1
        emit('chunk_ack', {'transferId': tid, 'index': d['index']})

@socketio.on('file_upload_complete')
def handle_upload_complete(d):
    tid = d.get('transferId')
    if tid not in active_transfers: return
    t = active_transfers[tid]; user = sessions[t['sid']]['user']
    content = b"".join([t['chunks'][i] for i in range(t['total'])])
    room = sessions[t['sid']]['room'] if t['mode'] == 'public' else 'private'
    conn = get_db()
    conn.execute("INSERT INTO files (filename, content, sender, room, filesize) VALUES (?, ?, ?, ?, ?)", (t['filename'], sqlite3.Binary(content), user, room, t['filesize']))
    conn.commit(); conn.close()
    if t['mode'] == 'public': socketio.emit('response', {'type': 'system', 'msg': f"BROADCAST: {user} shared '{t['filename']}'"}, room=room)
    else: emit('response', {'type': 'system', 'msg': f"VAULT: '{t['filename']}' saved."}, to=t['sid'])
    del active_transfers[tid]

def format_size(b):
    for u in ['B','KB','MB','GB']:
        if b < 1024: return f"{b:.1f} {u}"
        b /= 1024

if __name__ == '__main__':
    socketio.run(app, host='0.0.0.0', port=int(os.environ.get("PORT", 5000)))
