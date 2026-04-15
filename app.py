import eventlet
eventlet.monkey_patch()

import sqlite3
import os
import math
from flask import Flask, render_template, request
from flask_socketio import SocketIO, emit, join_room, leave_room

app = Flask(__name__)
app.config['SECRET_KEY'] = 'termnet_perfect_v4'
app.config['MAX_CONTENT_LENGTH'] = 100 * 1024 * 1024

# Set a massive buffer to handle 100MB files in one go
socketio = SocketIO(
    app, 
    cors_allowed_origins="*", 
    async_mode='eventlet', 
    max_http_buffer_size=100 * 1024 * 1024
)

DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'termnet.db')

def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn

def init_db():
    conn = get_db()
    c = conn.cursor()
    c.execute('CREATE TABLE IF NOT EXISTS users (username TEXT PRIMARY KEY)')
    c.execute('CREATE TABLE IF NOT EXISTS rooms (name TEXT PRIMARY KEY, password TEXT)')
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

@app.route('/')
def index(): return render_template('index.html')

@socketio.on('connect')
def handle_connect():
    sessions[request.sid] = {'user': 'guest', 'room': 'home'}

@socketio.on('command')
def handle_command(data):
    sid = request.sid
    if sid not in sessions: sessions[sid] = {'user': 'guest', 'room': 'home'}
    
    raw = data.get('command', '').strip()
    if not raw: return
    
    # Robust Parser: handle "jump r-123" or "jump r - 123"
    parts = raw.replace('-', ' - ').split()
    cmd = parts[0].lower()
    args = parts[1:]
    
    conn = get_db(); c = conn.cursor()
    try:
        # Cleanup old files
        c.execute("DELETE FROM files WHERE uploaded_at <= datetime('now', '-1 day')")
        conn.commit()

        if cmd == "help":
            msg = "\n[ TERMNET COMMANDS ]\n" + "-"*35 + \
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
            if not args: emit('response', {'type': 'error', 'msg': "Usage: login <name>"})
            else:
                u = args[0].lower()
                c.execute("SELECT username FROM users WHERE username=?", (u,))
                if c.fetchone():
                    sessions[sid]['user'] = u
                    emit('response', {'type': 'system', 'msg': f"Auth: {u}", 'user': u})
                else: emit('response', {'type': 'error', 'msg': "No user."})

        elif cmd == "list":
            c.execute("SELECT name FROM rooms")
            rs = "\n".join([f"  ▸ {r['name']}" for r in c.fetchall()])
            emit('response', {'type': 'system', 'msg': f"\n┌─ ROOMS ──┐\n{rs}\n└──────────┘"})

        elif cmd == "create":
            if len(args) < 3 or args[1] != '-': emit('response', {'type': 'error', 'msg': "Usage: create <n> - <p>"})
            else:
                n, p = args[0].lower(), args[2]
                try:
                    c.execute("INSERT INTO rooms VALUES (?, ?)", (n, p))
                    conn.commit()
                    emit('response', {'type': 'system', 'msg': f"Room '{n}' created."})
                except: emit('response', {'type': 'error', 'msg': "Exists."})

        elif cmd == "jump":
            if len(args) < 3 or args[1] != '-': emit('response', {'type': 'error', 'msg': "Usage: jump <n> - <p>"})
            else:
                n, p = args[0].lower(), args[2]
                c.execute("SELECT password FROM rooms WHERE name=?", (n,))
                row = c.fetchone()
                if row and str(row['password']) == str(p):
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
                emit('file_download', {'filename': row['filename'], 'content': row['content']})
            else: emit('response', {'type': 'error', 'msg': "Not found."})

        elif cmd == "share":
            if sessions[sid]['room'] == 'home': emit('response', {'type': 'error', 'msg': "Jump first."})
            else: emit('trigger_upload', {'mode': 'public'})

        elif cmd == "vault":
            if sessions[sid]['user'] == 'guest': emit('response', {'type': 'error', 'msg': "Login first."})
            else: emit('trigger_upload', {'mode': 'private'})

        elif cmd == "profile":
            u = sessions[sid]['user']
            c.execute("SELECT filename, filesize FROM files WHERE sender=? ORDER BY id DESC", (u,))
            fs = "\n".join([f"  │ ◈ {f['filename']} ({format_size(f['filesize'])})" for f in c.fetchall()])
            card = f"\n  ┌────────────────────────────┐\n  │ IDENTITY: {u.upper().ljust(16)} │\n  ├────────────────────────────┤\n{fs if fs else '  │ (Vault Empty)'}\n  └────────────────────────────┘"
            emit('response', {'type': 'system', 'msg': card})

        elif cmd == "exit":
            r = sessions[sid]['room']
            if r != 'home':
                leave_room(r); sessions[sid]['room'] = 'home'
                emit('response', {'type': 'system', 'msg': "Returned home.", 'room': 'home'})
        
        elif cmd == "msg":
            r = sessions[sid]['room']
            if r != 'home': socketio.emit('chat_msg', {'user': sessions[sid]['user'], 'msg': " ".join(args)}, room=r)

    except Exception as e: print(f"Error: {e}")
    finally: conn.close()

@socketio.on('direct_upload')
def handle_direct_upload(d):
    sid = request.sid; u = sessions[sid]['user']; r = sessions[sid]['room']
    mode, filename, content = d.get('mode'), d.get('filename'), d.get('content')
    if not u or u == 'guest': return
    
    target_room = r if mode == 'public' else 'private'
    conn = get_db()
    conn.execute("INSERT INTO files (filename, content, sender, room, filesize) VALUES (?, ?, ?, ?, ?)", 
                 (filename, sqlite3.Binary(content), u, target_room, len(content)))
    conn.commit(); conn.close()
    
    if mode == 'public':
        socketio.emit('response', {'type': 'system', 'msg': f"BROADCAST: {u} shared '{filename}'"}, room=r)
    else:
        emit('response', {'type': 'system', 'msg': f"VAULT: '{filename}' saved."})

def format_size(b):
    for u in ['B','KB','MB','GB']:
        if b < 1024: return f"{b:.1f} {u}"
        b /= 1024

if __name__ == '__main__':
    socketio.run(app, host='0.0.0.0', port=int(os.environ.get("PORT", 5000)))
