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
app.config['MAX_CONTENT_LENGTH'] = 50 * 1024 * 1024  # 50MB max

socketio = SocketIO(
    app, 
    cors_allowed_origins="*", 
    async_mode='eventlet', 
    ping_timeout=60,
    max_http_buffer_size=50 * 1024 * 1024  # 50MB for socket messages
)

# --- DATABASE LOGIC ---
DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'termnet.db')

def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")  # Faster concurrent reads
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
    # Migration: add filesize column to existing databases
    try:
        c.execute("ALTER TABLE files ADD COLUMN filesize INTEGER DEFAULT 0")
    except sqlite3.OperationalError:
        pass  # Column already exists
    conn.commit()
    conn.close()

init_db()

# Session store: { sid: { user, room } }
sessions = {}

# Chunked upload store: { transfer_id: { chunks[], total, received, filename, mode, sid } }
active_transfers = {}

# --- ROUTES ---
@app.route('/')
def index():
    return render_template('index.html')

@app.route('/guide')
def guide():
    return render_template('userguide.html')


# --- WEBSOCKET EVENTS ---
@socketio.on('connect')
def handle_connect():
    sessions[request.sid] = {'user': 'guest', 'room': 'home'}
    print(f"[+] Connected: {request.sid}")

@socketio.on('disconnect')
def handle_disconnect():
    sid = request.sid
    if sid in sessions:
        session = sessions[sid]
        if session['room'] != 'home':
            leave_room(session['room'])
        del sessions[sid]
    # Clean up any incomplete transfers for this sid
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
    if not raw:
        return
    
    parts = raw.split()
    cmd = parts[0].lower()
    args = parts[1:]
    
    conn = get_db()
    c = conn.cursor()

    try:
        # --- THE SAFE JANITOR (Auto-Delete Files older than 24 hours) ---
        c.execute("DELETE FROM files WHERE uploaded_at <= datetime('now', '-1 day')")
        conn.commit()

        # --- HELP ---
        if cmd == "help":
            msg = "\n[ TERMNET COMMANDS ]" + \
                  "\n" + "-" * 35 + \
                  "\n  signup/login <user>  : Identity" + \
                  "\n  list                 : Show Rooms" + \
                  "\n  create/jump <r>-<p>  : Room Management" + \
                  "\n  destroy <room_name>  : Remove a Room" + \
                  "\n  vault                : Private Save" + \
                  "\n  share                : Public Room Share" + \
                  "\n  fetch <filename>     : Download File" + \
                  "\n  profile              : View Your Vault" + \
                  "\n  exit / clear         : Navigation" + \
                  "\n" + "-" * 35
            emit('response', {'type': 'system', 'msg': msg})

        # --- SIGNUP ---
        elif cmd == "signup":
            if not args:
                emit('response', {'type': 'error', 'msg': "Usage: signup <name>"})
            else:
                username = args[0].lower()
                try:
                    c.execute("INSERT INTO users VALUES (?)", (username,))
                    conn.commit()
                    sessions[sid]['user'] = username
                    emit('response', {'type': 'system', 'msg': f"Identity registered: {username}", 'user': username})
                except sqlite3.IntegrityError:
                    emit('response', {'type': 'error', 'msg': "Name already taken."})

        # --- LOGIN ---
        elif cmd == "login":
            if not args:
                emit('response', {'type': 'error', 'msg': "Usage: login <name>"})
            else:
                username = args[0].lower()
                c.execute("SELECT username FROM users WHERE username=?", (username,))
                row = c.fetchone()
                if row:
                    sessions[sid]['user'] = username
                    emit('response', {'type': 'system', 'msg': f"Auth Success: {username}", 'user': username})
                else:
                    emit('response', {'type': 'error', 'msg': "User not found. Use 'signup' first."})

        # --- LIST ---
        elif cmd == "list":
            c.execute("SELECT name FROM rooms")
            rows = c.fetchall()
            room_list = "\n".join([f"  ▸ {row['name']}" for row in rows])
            # Count online users per room
            msg = f"\n┌─ ACTIVE ROOMS ─────────────┐\n{room_list}\n└────────────────────────────┘"
            emit('response', {'type': 'system', 'msg': msg})

        # --- CREATE ---
        elif cmd == "create":
            if len(args) < 3 or args[1] != '-':
                emit('response', {'type': 'error', 'msg': "Usage: create <name> - <pass>"})
            else:
                r_name, r_pass = args[0].lower(), args[2]
                try:
                    c.execute("INSERT INTO rooms VALUES (?, ?)", (r_name, r_pass))
                    conn.commit()
                    emit('response', {'type': 'system', 'msg': f"Room '{r_name}' deployed successfully."})
                except sqlite3.IntegrityError:
                    emit('response', {'type': 'error', 'msg': "Room already exists."})

        # --- DESTROY ---
        elif cmd == "destroy":
            if not args:
                emit('response', {'type': 'error', 'msg': "Usage: destroy <room_name>"})
            else:
                target_room = args[0].lower()
                if target_room == 'lobby':
                    emit('response', {'type': 'error', 'msg': "Cannot destroy system 'lobby'."})
                else:
                    c.execute("SELECT name FROM rooms WHERE name=?", (target_room,))
                    if c.fetchone():
                        # Also delete files associated with the room
                        c.execute("DELETE FROM files WHERE room=?", (target_room,))
                        c.execute("DELETE FROM rooms WHERE name=?", (target_room,))
                        conn.commit()
                        emit('response', {'type': 'system', 'msg': f"Room '{target_room}' has been wiped."})
                    else:
                        emit('response', {'type': 'error', 'msg': f"Room '{target_room}' not found."})

        # --- JUMP ---
        elif cmd == "jump":
            if len(args) < 3 or args[1] != '-':
                emit('response', {'type': 'error', 'msg': "Usage: jump <name> - <pass>"})
            else:
                r_name, r_pass = args[0].lower(), args[2]
                c.execute("SELECT password FROM rooms WHERE name=?", (r_name,))
                row = c.fetchone()
                if row and row['password'] == r_pass:
                    old = sessions[sid]['room']
                    if old != 'home':
                        leave_room(old)
                    join_room(r_name)
                    sessions[sid]['room'] = r_name
                    # Notify room members
                    user = sessions[sid]['user']
                    emit('chat_msg', {'user': '⚡SYSTEM', 'msg': f'{user} has entered the room.'}, to=r_name, include_self=False)
                    emit('response', {'type': 'jump_success', 'msg': f"Connection established: {r_name}", 'room': r_name})
                else:
                    emit('response', {'type': 'error', 'msg': "Access Denied. Invalid room or password."})

        # --- WHO (New: see who's in the room) ---
        elif cmd == "who":
            current_room = sessions[sid]['room']
            if current_room == 'home':
                emit('response', {'type': 'error', 'msg': "Jump to a room first."})
            else:
                users_in_room = [s['user'] for s in sessions.values() if s['room'] == current_room]
                user_list = "\n".join([f"  ◉ {u}" for u in users_in_room])
                msg = f"\n┌─ ONLINE IN '{current_room}' ──┐\n{user_list}\n└─────────────────────────┘"
                emit('response', {'type': 'system', 'msg': msg})

        # --- VAULT / SHARE ---
        elif cmd == "vault":
            if sessions[sid]['user'] == 'guest':
                emit('response', {'type': 'error', 'msg': "Login required."})
            else:
                emit('trigger_upload', {'mode': 'private'})
        
        elif cmd == "share":
            if sessions[sid]['user'] == 'guest':
                emit('response', {'type': 'error', 'msg': "Login required."})
            elif sessions[sid]['room'] == 'home':
                emit('response', {'type': 'error', 'msg': "Jump to a room first."})
            else:
                emit('trigger_upload', {'mode': 'public'})

        # --- FETCH ---
        elif cmd == "fetch":
            if not args:
                emit('response', {'type': 'error', 'msg': "Usage: fetch <filename>"})
            else:
                full_filename = " ".join(args).strip()
                user = sessions[sid]['user']
                current_room = sessions[sid]['room']
                
                # Search case-insensitively and handle spaces correctly
                c.execute("""SELECT filename, content, filesize FROM files 
                            WHERE LOWER(filename)=LOWER(?) AND (room=? OR (room='private' AND sender=?))
                            ORDER BY id DESC LIMIT 1""", 
                         (full_filename, current_room, user))
                row = c.fetchone()
                if row:
                    content = row['content']
                    filesize = row['filesize'] or len(content)
                    # Send file in chunks for progress tracking
                    chunk_size = 1024 * 1024  # 1MB chunks
                    total_chunks = max(1, math.ceil(len(content) / chunk_size))
                    
                    emit('file_download_start', {
                        'filename': row['filename'],
                        'totalChunks': total_chunks,
                        'filesize': filesize
                    })
                    
                    for i in range(total_chunks):
                        chunk = content[i * chunk_size : (i + 1) * chunk_size]
                        emit('file_download_chunk', {
                            'filename': row['filename'],
                            'chunk': chunk,
                            'index': i,
                            'total': total_chunks
                        })
                        eventlet.sleep(0)  # Yield to allow progress updates
                    
                    emit('file_download_complete', {'filename': row['filename']})
                else:
                    emit('response', {'type': 'error', 'msg': f"File '{full_filename}' not found in scope."})

        # --- PROFILE ---
        elif cmd == "profile":
            user = sessions[sid]['user']
            if user == 'guest':
                emit('response', {'type': 'error', 'msg': "Login required."})
            else:
                c.execute("SELECT filename, filesize, uploaded_at FROM files WHERE sender=? ORDER BY uploaded_at DESC", (user,))
                files = c.fetchall()
                if files:
                    f_list = "\n".join([f"  │ ◈ {f['filename']} ({format_size(f['filesize'] or 0)})" for f in files])
                else:
                    f_list = "  │ (Vault Empty)"
                card = f"\n  ┌────────────────────────────┐\n  │ IDENTITY: {user.upper().ljust(16)} │\n  ├────────────────────────────┤\n{f_list}\n  └────────────────────────────┘"
                emit('response', {'type': 'system', 'msg': card})

        # --- EXIT ---
        elif cmd == "exit":
            if sessions[sid]['room'] != 'home':
                user = sessions[sid]['user']
                room = sessions[sid]['room']
                emit('chat_msg', {'user': '⚡SYSTEM', 'msg': f'{user} has left the room.'}, to=room, include_self=False)
                leave_room(room)
                sessions[sid]['room'] = 'home'
                emit('response', {'type': 'system', 'msg': "Disconnected. Returned to home.", 'room': 'home'})
            else:
                emit('response', {'type': 'system', 'msg': "Already at home."})

        # --- MSG ---
        elif cmd == "msg":
            if sessions[sid]['room'] == 'home':
                emit('response', {'type': 'error', 'msg': "Jump to a room first to send messages."})
            elif not args:
                emit('response', {'type': 'error', 'msg': "Usage: msg <your message>"})
            else:
                emit('chat_msg', {'user': sessions[sid]['user'], 'msg': " ".join(args)}, to=sessions[sid]['room'])

        # --- UNKNOWN COMMAND ---
        else:
            emit('response', {'type': 'error', 'msg': f"Unknown command: '{cmd}'. Type 'help' for available commands."})

    except Exception as e:
        print(f"[!] Error processing command '{cmd}': {e}")
        emit('response', {'type': 'error', 'msg': "Internal Terminal Error."})
    finally:
        conn.close()


# --- CHUNKED FILE UPLOAD HANDLERS ---
@socketio.on('file_upload_start')
def handle_upload_start(data):
    """Initialize a chunked file upload."""
    sid = request.sid
    if sid not in sessions:
        return
    
    transfer_id = str(uuid.uuid4())[:8]
    active_transfers[transfer_id] = {
        'chunks': {},
        'total': data['totalChunks'],
        'received': 0,
        'filename': data['filename'],
        'filesize': data['filesize'],
        'mode': data['mode'],
        'sid': sid
    }
    emit('upload_ready', {'transferId': transfer_id})


@socketio.on('file_upload_chunk')
def handle_upload_chunk(data):
    """Receive a chunk of a file upload."""
    sid = request.sid
    transfer_id = data.get('transferId')
    
    if transfer_id not in active_transfers:
        return
    
    transfer = active_transfers[transfer_id]
    idx = data['index']
    transfer['chunks'][idx] = data['chunk']
    transfer['received'] += 1


@socketio.on('file_upload_complete')
def handle_upload_complete(data):
    """Finalize a chunked file upload — reassemble and store."""
    sid = request.sid
    transfer_id = data.get('transferId')
    
    if transfer_id not in active_transfers:
        return
    
    transfer = active_transfers[transfer_id]
    
    # Fast reassembly
    content = "".join([transfer['chunks'][i] for i in range(transfer['total'])])
    
    filename = transfer['filename']
    filesize = transfer['filesize']
    mode = transfer['mode']
    room = sessions[sid]['room'] if mode == 'public' else 'private'
    user = sessions[sid]['user']
    
    conn = get_db()
    try:
        conn.execute(
            "INSERT INTO files (filename, content, sender, room, filesize) VALUES (?, ?, ?, ?, ?)",
            (filename, content, user, room, filesize)
        )
        conn.commit()
        
        if mode == 'public':
            socketio.emit('response', {
                'type': 'system', 
                'msg': f"BROADCAST: {user} shared a file -> '{filename}' ({format_size(filesize)})"
            }, room=room)
        else:
            emit('response', {
                'type': 'system', 
                'msg': f"SECURED: '{filename}' ({format_size(filesize)}) added to vault."
            })
    except Exception as e:
        print(f"[!] File save error: {e}")
    finally:
        conn.close()
        # Cleanup transfer
        if transfer_id in active_transfers:
            del active_transfers[transfer_id]


def format_size(size_bytes):
    """Format bytes to human readable string."""
    if size_bytes == 0:
        return "0 B"
    units = ['B', 'KB', 'MB', 'GB']
    i = 0
    size = float(size_bytes)
    while size >= 1024 and i < len(units) - 1:
        size /= 1024
        i += 1
    return f"{size:.1f} {units[i]}"


if __name__ == '__main__':
    port = int(os.environ.get("PORT", 5000))
    socketio.run(app, host='0.0.0.0', port=port, debug=False)