import eventlet
eventlet.monkey_patch()

import sqlite3
import os
import uuid
import shutil
from flask import Flask, render_template, request, jsonify, send_file, abort
from flask_socketio import SocketIO, emit, join_room, leave_room
from werkzeug.utils import secure_filename

app = Flask(__name__)
app.config['SECRET_KEY'] = 'termnet_ultimate_v5'
app.config['MAX_CONTENT_LENGTH'] = 100 * 1024 * 1024  # 100MB max upload

# Files stored on disk, NOT in database
UPLOAD_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'uploads')
os.makedirs(UPLOAD_DIR, exist_ok=True)

socketio = SocketIO(
    app,
    cors_allowed_origins="*",
    async_mode='eventlet',
    ping_timeout=120,
    ping_interval=25
)

# --- DATABASE (metadata only, no file content) ---
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
    c.execute('''CREATE TABLE IF NOT EXISTS file_meta
                 (id TEXT PRIMARY KEY,
                  filename TEXT,
                  filepath TEXT,
                  sender TEXT,
                  room TEXT,
                  filesize INTEGER DEFAULT 0,
                  uploaded_at DATETIME DEFAULT CURRENT_TIMESTAMP)''')
    c.execute("INSERT OR IGNORE INTO rooms VALUES ('lobby', '123')")
    conn.commit()
    conn.close()

init_db()

sessions = {}  # sid -> {user, room}

def format_size(b):
    if b is None or b == 0:
        return "0 B"
    for u in ['B', 'KB', 'MB', 'GB']:
        if b < 1024:
            return f"{b:.1f} {u}"
        b /= 1024
    return f"{b:.1f} TB"


# ══════════════════════════════════════
# HTTP ROUTES (Pages + File Transfer)
# ══════════════════════════════════════

@app.route('/')
def index():
    return render_template('index.html')

@app.route('/guide')
def guide():
    return render_template('userguide.html')


@app.route('/upload', methods=['POST'])
def http_upload():
    """HTTP file upload — fast, with real browser progress tracking."""
    f = request.files.get('file')
    sender = request.form.get('sender', 'guest')
    room = request.form.get('room', 'home')
    mode = request.form.get('mode', 'public')

    if not f or not f.filename:
        return jsonify({'error': 'No file provided'}), 400

    if sender == 'guest':
        return jsonify({'error': 'Login required'}), 403

    # Generate unique ID and save to disk
    file_id = str(uuid.uuid4())
    safe_name = secure_filename(f.filename) or 'file'
    # Keep original name for display but use UUID for storage
    disk_name = f"{file_id}_{safe_name}"
    disk_path = os.path.join(UPLOAD_DIR, disk_name)

    f.save(disk_path)
    filesize = os.path.getsize(disk_path)

    # Determine storage room
    target_room = 'private' if mode == 'private' else room

    # Save metadata to DB
    conn = get_db()
    conn.execute(
        "INSERT INTO file_meta (id, filename, filepath, sender, room, filesize) VALUES (?, ?, ?, ?, ?, ?)",
        (file_id, f.filename, disk_name, sender, target_room, filesize)
    )
    conn.commit()
    conn.close()

    # Notify via WebSocket
    if mode == 'public' and room != 'home':
        socketio.emit('response', {
            'type': 'system',
            'msg': f"BROADCAST: {sender} shared '{f.filename}' ({format_size(filesize)})"
        }, room=room)
    else:
        # Find the sid for this user to send private notification
        for sid, sess in sessions.items():
            if sess['user'] == sender:
                socketio.emit('response', {
                    'type': 'system',
                    'msg': f"VAULT: '{f.filename}' ({format_size(filesize)}) saved."
                }, to=sid)
                break

    return jsonify({
        'ok': True,
        'id': file_id,
        'filename': f.filename,
        'size': filesize
    })


@app.route('/download/<file_id>')
def http_download(file_id):
    """HTTP file download — fast, streamed directly from disk."""
    conn = get_db()
    row = conn.execute("SELECT filename, filepath FROM file_meta WHERE id=?", (file_id,)).fetchone()
    conn.close()

    if not row:
        abort(404)

    disk_path = os.path.join(UPLOAD_DIR, row['filepath'])
    if not os.path.exists(disk_path):
        abort(404)

    return send_file(disk_path, as_attachment=True, download_name=row['filename'])


# ══════════════════════════════════════
# WEBSOCKET (Commands + Chat ONLY)
# ══════════════════════════════════════

@socketio.on('connect')
def handle_connect():
    sessions[request.sid] = {'user': 'guest', 'room': 'home'}

@socketio.on('restore_session')
def handle_restore(data):
    sid = request.sid
    u = data.get('user', 'guest')
    r = data.get('room', 'home')
    sessions[sid] = {'user': u, 'room': r}
    if r != 'home':
        join_room(r)

@socketio.on('disconnect')
def handle_disconnect():
    sid = request.sid
    if sid in sessions:
        del sessions[sid]


@socketio.on('command')
def handle_command(data):
    sid = request.sid
    if sid not in sessions:
        sessions[sid] = {'user': 'guest', 'room': 'home'}

    raw = data.get('command', '').strip()
    if not raw:
        return

    parts = raw.split()
    cmd = parts[0].lower()
    args = parts[1:]

    conn = get_db()
    c = conn.cursor()

    try:
        # Auto-cleanup old files (24h)
        c.execute("SELECT id, filepath FROM file_meta WHERE uploaded_at <= datetime('now', '-1 day')")
        old_files = c.fetchall()
        for of in old_files:
            disk_path = os.path.join(UPLOAD_DIR, of['filepath'])
            if os.path.exists(disk_path):
                os.remove(disk_path)
        c.execute("DELETE FROM file_meta WHERE uploaded_at <= datetime('now', '-1 day')")
        conn.commit()

        # ── HELP ──
        if cmd == "help":
            msg = (
                "\n[ TERMNET COMMANDS ]\n" + "-" * 35 +
                "\n  signup <user>       : Create Identity"
                "\n  login <user>        : Authenticate"
                "\n  list                : Show Rooms"
                "\n  create <r> - <p>    : Create Room"
                "\n  jump <r> - <p>      : Join Room"
                "\n  destroy <room>      : Delete Room"
                "\n  msg <text>          : Send Message"
                "\n  who                 : Users Online"
                "\n  share               : Upload to Room"
                "\n  vault               : Private Upload"
                "\n  files               : List Files"
                "\n  fetch <filename>    : Download File"
                "\n  profile             : Your Identity"
                "\n  exit                : Leave Room"
                "\n  clear               : Wipe Terminal"
                "\n" + "-" * 35
            )
            emit('response', {'type': 'system', 'msg': msg})

        # ── SIGNUP ──
        elif cmd == "signup":
            if not args:
                emit('response', {'type': 'error', 'msg': "Usage: signup <name>"})
            else:
                u = args[0].lower()
                try:
                    c.execute("INSERT INTO users VALUES (?)", (u,))
                    conn.commit()
                    sessions[sid]['user'] = u
                    emit('response', {'type': 'system', 'msg': f"Identity registered: {u}", 'user': u})
                except sqlite3.IntegrityError:
                    emit('response', {'type': 'error', 'msg': "Name already taken."})

        # ── LOGIN ──
        elif cmd == "login":
            if not args:
                emit('response', {'type': 'error', 'msg': "Usage: login <name>"})
            else:
                u = args[0].lower()
                c.execute("SELECT username FROM users WHERE username=?", (u,))
                if c.fetchone():
                    sessions[sid]['user'] = u
                    emit('response', {'type': 'system', 'msg': f"Auth Success: {u}", 'user': u})
                else:
                    emit('response', {'type': 'error', 'msg': "User not found. Use 'signup' first."})

        # ── LIST ──
        elif cmd == "list":
            c.execute("SELECT name FROM rooms")
            rs = "\n".join([f"  > {r['name']}" for r in c.fetchall()])
            emit('response', {'type': 'system', 'msg': f"\n[ ACTIVE ROOMS ]\n{'-'*20}\n{rs}\n{'-'*20}"})

        # ── CREATE ──
        elif cmd == "create":
            if len(args) < 3 or '-' not in args:
                emit('response', {'type': 'error', 'msg': "Usage: create <name> - <pass>"})
            else:
                dash_idx = args.index('-')
                n = args[0].lower()
                p = args[dash_idx + 1] if dash_idx + 1 < len(args) else ''
                try:
                    c.execute("INSERT INTO rooms VALUES (?, ?)", (n, p))
                    conn.commit()
                    emit('response', {'type': 'system', 'msg': f"Room '{n}' created."})
                except sqlite3.IntegrityError:
                    emit('response', {'type': 'error', 'msg': "Room already exists."})

        # ── JUMP ──
        elif cmd == "jump":
            if len(args) < 3 or '-' not in args:
                emit('response', {'type': 'error', 'msg': "Usage: jump <name> - <pass>"})
            else:
                dash_idx = args.index('-')
                n = args[0].lower()
                p = args[dash_idx + 1] if dash_idx + 1 < len(args) else ''
                c.execute("SELECT password FROM rooms WHERE name=?", (n,))
                row = c.fetchone()
                if row and str(row['password']) == str(p):
                    old = sessions[sid]['room']
                    if old != 'home':
                        leave_room(old)
                    join_room(n)
                    sessions[sid]['room'] = n
                    user = sessions[sid]['user']
                    emit('chat_msg', {'user': 'SYSTEM', 'msg': f'{user} joined.'}, to=n, include_self=False)
                    emit('response', {'type': 'system', 'msg': f"Connected: {n}", 'room': n})
                else:
                    emit('response', {'type': 'error', 'msg': "Access Denied."})

        # ── DESTROY ──
        elif cmd == "destroy":
            if not args:
                emit('response', {'type': 'error', 'msg': "Usage: destroy <room>"})
            else:
                target = args[0].lower()
                if target == 'lobby':
                    emit('response', {'type': 'error', 'msg': "Cannot destroy 'lobby'."})
                else:
                    c.execute("SELECT name FROM rooms WHERE name=?", (target,))
                    if c.fetchone():
                        # Delete associated files from disk
                        c.execute("SELECT filepath FROM file_meta WHERE room=?", (target,))
                        for f in c.fetchall():
                            fp = os.path.join(UPLOAD_DIR, f['filepath'])
                            if os.path.exists(fp):
                                os.remove(fp)
                        c.execute("DELETE FROM file_meta WHERE room=?", (target,))
                        c.execute("DELETE FROM rooms WHERE name=?", (target,))
                        conn.commit()
                        emit('response', {'type': 'system', 'msg': f"Room '{target}' destroyed."})
                    else:
                        emit('response', {'type': 'error', 'msg': f"Room '{target}' not found."})

        # ── WHO ──
        elif cmd == "who":
            room = sessions[sid]['room']
            if room == 'home':
                emit('response', {'type': 'error', 'msg': "Jump to a room first."})
            else:
                users = [s['user'] for s in sessions.values() if s['room'] == room]
                ulist = "\n".join([f"  > {u}" for u in users])
                emit('response', {'type': 'system', 'msg': f"\n[ ONLINE IN '{room}' ]\n{'-'*20}\n{ulist}\n{'-'*20}"})

        # ── MSG ──
        elif cmd == "msg":
            room = sessions[sid]['room']
            if room == 'home':
                emit('response', {'type': 'error', 'msg': "Jump to a room first."})
            elif not args:
                emit('response', {'type': 'error', 'msg': "Usage: msg <text>"})
            else:
                socketio.emit('chat_msg', {
                    'user': sessions[sid]['user'],
                    'msg': " ".join(args)
                }, room=room)

        # ── SHARE ──
        elif cmd == "share":
            if sessions[sid]['user'] == 'guest':
                emit('response', {'type': 'error', 'msg': "Login first."})
            elif sessions[sid]['room'] == 'home':
                emit('response', {'type': 'error', 'msg': "Jump to a room first."})
            else:
                emit('trigger_upload', {'mode': 'public'})

        # ── VAULT ──
        elif cmd == "vault":
            if sessions[sid]['user'] == 'guest':
                emit('response', {'type': 'error', 'msg': "Login first."})
            else:
                emit('trigger_upload', {'mode': 'private'})

        # ── FILES ──
        elif cmd == "files":
            room = sessions[sid]['room']
            user = sessions[sid]['user']
            # Show room files + own private files
            c.execute("""SELECT id, filename, filesize, sender FROM file_meta
                        WHERE room=? OR (room='private' AND sender=?)
                        ORDER BY uploaded_at DESC""", (room, user))
            rows = c.fetchall()
            if rows:
                flist = "\n".join([f"  > {f['filename']} ({format_size(f['filesize'])}) [{f['sender']}]" for f in rows])
                emit('response', {'type': 'system', 'msg': f"\n[ FILES ]\n{'-'*30}\n{flist}\n{'-'*30}"})
            else:
                emit('response', {'type': 'system', 'msg': "No files found."})

        # ── FETCH ──
        elif cmd == "fetch":
            if not args:
                emit('response', {'type': 'error', 'msg': "Usage: fetch <filename>"})
            else:
                fn = " ".join(args).strip()
                user = sessions[sid]['user']
                room = sessions[sid]['room']
                c.execute("""SELECT id, filename, filesize FROM file_meta
                            WHERE LOWER(filename)=LOWER(?)
                            AND (room=? OR (room='private' AND sender=?))
                            ORDER BY uploaded_at DESC LIMIT 1""", (fn, room, user))
                row = c.fetchone()
                if row:
                    # Send download URL to client — browser handles the rest
                    emit('file_download', {
                        'url': f"/download/{row['id']}",
                        'filename': row['filename'],
                        'filesize': row['filesize']
                    })
                else:
                    emit('response', {'type': 'error', 'msg': f"File '{fn}' not found."})

        # ── PROFILE ──
        elif cmd == "profile":
            user = sessions[sid]['user']
            if user == 'guest':
                emit('response', {'type': 'error', 'msg': "Login first."})
            else:
                c.execute("SELECT filename, filesize FROM file_meta WHERE sender=? ORDER BY uploaded_at DESC", (user,))
                files = c.fetchall()
                if files:
                    flist = "\n".join([f"  | {f['filename']} ({format_size(f['filesize'])})" for f in files])
                else:
                    flist = "  | (empty)"
                card = f"\n  .{'─'*28}.\n  | IDENTITY: {user.upper().ljust(16)}|\n  |{'─'*28}|\n{flist}\n  '{'─'*28}'"
                emit('response', {'type': 'system', 'msg': card})

        # ── EXIT ──
        elif cmd == "exit":
            room = sessions[sid]['room']
            if room != 'home':
                user = sessions[sid]['user']
                emit('chat_msg', {'user': 'SYSTEM', 'msg': f'{user} left.'}, to=room, include_self=False)
                leave_room(room)
                sessions[sid]['room'] = 'home'
                emit('response', {'type': 'system', 'msg': "Returned home.", 'room': 'home'})
            else:
                emit('response', {'type': 'system', 'msg': "Already home."})

        # ── UNKNOWN COMMAND ──
        else:
            emit('response', {'type': 'error', 'msg': f"Unknown: '{cmd}'. Type 'help' for commands."})

    except Exception as e:
        print(f"[!] Error in '{cmd}': {e}")
        import traceback
        traceback.print_exc()
        emit('response', {'type': 'error', 'msg': "Internal error."})
    finally:
        conn.close()


if __name__ == '__main__':
    port = int(os.environ.get("PORT", 5000))
    socketio.run(app, host='0.0.0.0', port=port, debug=False)
