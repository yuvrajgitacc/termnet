import eventlet
eventlet.monkey_patch()

import sqlite3
import os
import uuid
import math
from flask import Flask, render_template, request
from flask_socketio import SocketIO, emit, join_room, leave_room
from datetime import datetime

app = Flask(__name__)
app.config['SECRET_KEY'] = 'termnet_ultimate_edition'
app.config['MAX_CONTENT_LENGTH'] = 100 * 1024 * 1024  # 100MB max

socketio = SocketIO(
    app, 
    cors_allowed_origins="*", 
    async_mode='eventlet', 
    ping_timeout=120,
    ping_interval=25,
    max_http_buffer_size=100 * 1024 * 1024
)

# --- DATABASE LOGIC ---
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
    c.execute('''CREATE TABLE IF NOT EXISTS files 
                 (id INTEGER PRIMARY KEY AUTOINCREMENT, 
                  filename TEXT, content TEXT, sender TEXT, room TEXT,
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
    print(f"[+] Connected: {request.sid}")

@socketio.on('restore_session')
def handle_restore(data):
    sid = request.sid
    user = data.get('user', 'guest')
    room = data.get('room', 'home')
    sessions[sid] = {'user': user, 'room': room}
    if room != 'home':
        join_room(room)
    print(f"[*] Restored: {user} in {room}")

@socketio.on('disconnect')
def handle_disconnect():
    sid = request.sid
    if sid in sessions:
        del sessions[sid]
    stale = [tid for tid, t in active_transfers.items() if t.get('sid') == sid]
    for tid in stale:
        del active_transfers[tid]
    print(f"[-] Disconnected: {sid}")

@socketio.on('command')
def handle_command(data):
    sid = request.sid
    if sid not in sessions:
        sessions[sid] = {'user': 'guest', 'room': 'home'}
        
    raw = data.get('command', '').strip()
    if not raw: return
    
    parts = raw.split()
    cmd = parts[0].lower()
    args = parts[1:]
    
    conn = get_db()
    c = conn.cursor()

    try:
        c.execute("DELETE FROM files WHERE uploaded_at <= datetime('now', '-1 day')")
        conn.commit()

        if cmd == "help":
            msg = "\n[ TERMNET COMMANDS ]" + \
                  "\n" + "-" * 35 + \
                  "\n  signup/login <user>  : Identity" + \
                  "\n  list                 : Show Rooms" + \
                  "\n  create/jump <r>-<p>  : Room Management" + \
                  "\n  destroy <room_name>  : Remove a Room" + \
                  "\n  vault                : Private Save" + \
                  "\n  share                : Public Room Share" + \
                  "\n  files                : List Room Files" + \
                  "\n  fetch <filename>     : Download File" + \
                  "\n  profile              : View Your Vault" + \
                  "\n  exit / clear         : Navigation" + \
                  "\n" + "-" * 35
            emit('response', {'type': 'system', 'msg': msg})

        elif cmd == "signup":
            if not args: emit('response', {'type': 'error', 'msg': "Usage: signup <name>"})
            else:
                username = args[0].lower()
                try:
                    c.execute("INSERT INTO users VALUES (?)", (username,))
                    conn.commit()
                    sessions[sid]['user'] = username
                    emit('response', {'type': 'system', 'msg': f"Identity registered: {username}", 'user': username})
                except sqlite3.IntegrityError:
                    emit('response', {'type': 'error', 'msg': "Name taken."})

        elif cmd == "login":
            if not args: emit('response', {'type': 'error', 'msg': "Usage: login <name>"})
            else:
                username = args[0].lower()
                c.execute("SELECT username FROM users WHERE username=?", (username,))
                if c.fetchone():
                    sessions[sid]['user'] = username
                    emit('response', {'type': 'system', 'msg': f"Auth Success: {username}", 'user': username})
                else:
                    emit('response', {'type': 'error', 'msg': "User not found."})

        elif cmd == "list":
            c.execute("SELECT name FROM rooms")
            room_list = "\n".join([f"  ▸ {row['name']}" for row in c.fetchall()])
            msg = f"\n┌─ ACTIVE ROOMS ─────────────┐\n{room_list}\n└────────────────────────────┘"
            emit('response', {'type': 'system', 'msg': msg})

        elif cmd == "create":
            if len(args) < 3 or args[1] != '-': emit('response', {'type': 'error', 'msg': "Usage: create <n> - <p>"})
            else:
                r_n, r_p = args[0].lower(), args[2]
                try:
                    c.execute("INSERT INTO rooms VALUES (?, ?)", (r_n, r_p))
                    conn.commit()
                    emit('response', {'type': 'system', 'msg': f"Room '{r_n}' deployed."})
                except sqlite3.IntegrityError: emit('response', {'type': 'error', 'msg': "Exists."})

        elif cmd == "jump":
            if len(args) < 3 or args[1] != '-': emit('response', {'type': 'error', 'msg': "Usage: jump <n> - <p>"})
            else:
                r_n, r_p = args[0].lower(), args[2]
                c.execute("SELECT password FROM rooms WHERE name=?", (r_n,))
                row = c.fetchone()
                if row and row['password'] == r_p:
                    old = sessions[sid]['room']
                    if old != 'home': leave_room(old)
                    join_room(r_n)
                    sessions[sid]['room'] = r_n
                    emit('response', {'type': 'system', 'msg': f"Connected: {r_n}", 'room': r_n})
                else: emit('response', {'type': 'error', 'msg': "Denied."})

        elif cmd == "files":
            room = sessions[sid]['room']
            if room == 'home': emit('response', {'type': 'error', 'msg': "Jump to a room."})
            else:
                c.execute("SELECT filename, filesize, sender FROM files WHERE room=? ORDER BY id DESC", (room,))
                f_list = "\n".join([f"  ▸ {f['filename']} ({format_size(f['filesize'])}) [by {f['sender']}]" for f in c.fetchall()])
                msg = f"\n┌─ SHARED IN '{room}' ──┐\n{f_list}\n└──────────────────────────┘" if f_list else "[!] No files."
                emit('response', {'type': 'system', 'msg': msg})

        elif cmd == "fetch":
            if not args: emit('response', {'type': 'error', 'msg': "Usage: fetch <name>"})
            else:
                fname = " ".join(args).strip()
                user = sessions[sid]['user']
                room = sessions[sid]['room']
                c.execute("SELECT filename, content, filesize FROM files WHERE LOWER(filename)=LOWER(?) AND (room=? OR (room='private' AND sender=?)) ORDER BY id DESC LIMIT 1", (fname, room, user))
                row = c.fetchone()
                if row:
                    content = row['content']
                    chunk_size = 1024 * 1024
                    total = math.ceil(len(content) / chunk_size)
                    emit('file_download_start', {'filename': row['filename'], 'totalChunks': total})
                    for i in range(total):
                        chunk = content[i*chunk_size : (i+1)*chunk_size]
                        emit('file_download_chunk', {'filename': row['filename'], 'chunk': chunk, 'index': i})
                        eventlet.sleep(0)
                    emit('file_download_complete', {'filename': row['filename']})
                else: emit('response', {'type': 'error', 'msg': "Not found."})

        elif cmd == "vault":
            if sessions[sid]['user'] == 'guest': emit('response', {'type': 'error', 'msg': "Login required."})
            else: emit('trigger_upload', {'mode': 'private'})
        
        elif cmd == "share":
            if sessions[sid]['room'] == 'home': emit('response', {'type': 'error', 'msg': "Jump to a room."})
            else: emit('trigger_upload', {'mode': 'public'})

        elif cmd == "profile":
            user = sessions[sid]['user']
            c.execute("SELECT filename, filesize FROM files WHERE sender=? ORDER BY id DESC", (user,))
            f_list = "\n".join([f"  │ ◈ {f['filename']} ({format_size(f['filesize'])})" for f in c.fetchall()])
            card = f"\n  ┌────────────────────────────┐\n  │ IDENTITY: {user.upper().ljust(16)} │\n  ├────────────────────────────┤\n{f_list}\n  └────────────────────────────┘"
            emit('response', {'type': 'system', 'msg': card})

        elif cmd == "exit":
            room = sessions[sid]['room']
            if room != 'home':
                leave_room(room)
                sessions[sid]['room'] = 'home'
                emit('response', {'type': 'system', 'msg': "Returned to home.", 'room': 'home'})
            else: emit('response', {'type': 'system', 'msg': "At home."})

        elif cmd == "msg":
            room = sessions[sid]['room']
            if room == 'home': emit('response', {'type': 'error', 'msg': "Jump first."})
            else: emit('chat_msg', {'user': sessions[sid]['user'], 'msg': " ".join(args)}, to=room)

        else: emit('response', {'type': 'error', 'msg': f"Unknown: '{cmd}'"})

    except Exception as e: print(f"[!] Error: {e}")
    finally: conn.close()

@socketio.on('file_upload_start')
def handle_upload_start(data):
    sid = request.sid
    tid = str(uuid.uuid4())[:8]
    active_transfers[tid] = {'chunks': {}, 'total': data['totalChunks'], 'received': 0, 'filename': data['filename'], 'filesize': data['filesize'], 'mode': data['mode'], 'sid': sid}
    emit('upload_ready', {'transferId': tid})

@socketio.on('file_upload_chunk')
def handle_upload_chunk(data):
    tid = data.get('transferId')
    if tid in active_transfers:
        active_transfers[tid]['chunks'][data['index']] = data['chunk']
        active_transfers[tid]['received'] += 1
        emit('chunk_ack', {'transferId': tid, 'index': data['index']})

@socketio.on('file_upload_complete')
def handle_upload_complete(data):
    tid = data.get('transferId')
    if tid not in active_transfers: return
    t = active_transfers[tid]
    content = "".join([t['chunks'][i] for i in range(t['total'])])
    room = sessions[t['sid']]['room'] if t['mode'] == 'public' else 'private'
    conn = get_db()
    conn.execute("INSERT INTO files (filename, content, sender, room, filesize) VALUES (?, ?, ?, ?, ?)", (t['filename'], content, sessions[t['sid']]['user'], room, t['filesize']))
    conn.commit()
    conn.close()
    if t['mode'] == 'public':
        socketio.emit('response', {'type': 'system', 'msg': f"BROADCAST: {sessions[t['sid']]['user']} shared '{t['filename']}'"}, room=room)
    else:
        emit('response', {'type': 'system', 'msg': f"SECURED: '{t['filename']}' in vault."}, to=t['sid'])
    del active_transfers[tid]

def format_size(b):
    for u in ['B','KB','MB','GB']:
        if b < 1024: return f"{b:.1f} {u}"
        b /= 1024

if __name__ == '__main__':
    socketio.run(app, host='0.0.0.0', port=int(os.environ.get("PORT", 5000)), debug=False)
