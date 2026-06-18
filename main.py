#!/usr/bin/env python3
"""
ZGOS — Python Launcher v3
HTTP + WebSocket (Party/Visio) + endpoint d'installation de jeux
Usage: python main.py
"""

import http.server
import socketserver
import webbrowser
import threading
import os, sys, json, time, struct, hashlib, base64
from urllib.parse import urlparse, parse_qs
try:
    from urllib.request import urlopen, Request
    from urllib.error import URLError
except ImportError:
    pass

PORT_HTTP = 8000
PORT_WS   = 8001
HOST      = "localhost"
GAMES_DIR = "installed_games"

# ─── WEBSOCKET ───
WS_MAGIC = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"
parties  = {}

def ws_handshake(conn, raw):
    key = ""
    for line in raw.split("\r\n"):
        if "Sec-WebSocket-Key" in line:
            key = line.split(": ")[1].strip()
    accept = base64.b64encode(hashlib.sha1((key + WS_MAGIC).encode()).digest()).decode()
    conn.sendall((
        "HTTP/1.1 101 Switching Protocols\r\n"
        "Upgrade: websocket\r\nConnection: Upgrade\r\n"
        f"Sec-WebSocket-Accept: {accept}\r\n\r\n"
    ).encode())

def ws_decode(data):
    if len(data) < 6: return None
    b2 = data[1]; masked = (b2 & 0x80) != 0; length = b2 & 0x7F; idx = 2
    if length == 126: length = struct.unpack(">H", data[idx:idx+2])[0]; idx += 2
    elif length == 127: length = struct.unpack(">Q", data[idx:idx+8])[0]; idx += 8
    if masked:
        mask = data[idx:idx+4]; idx += 4
        payload = bytearray(data[idx:idx+length])
        for i in range(len(payload)): payload[i] ^= mask[i % 4]
        return bytes(payload)
    return data[idx:idx+length]

def ws_encode(msg):
    data = msg.encode(); length = len(data)
    header = bytearray([0x81])
    if length < 126: header.append(length)
    elif length < 65536: header += bytearray([126]) + struct.pack(">H", length)
    else: header += bytearray([127]) + struct.pack(">Q", length)
    return bytes(header) + data

def ws_send(conn, msg):
    try: conn.sendall(ws_encode(msg))
    except: pass

def handle_ws_client(conn, addr):
    client_code = None
    try:
        raw = conn.recv(4096).decode("utf-8", errors="ignore")
        if "Upgrade: websocket" not in raw: conn.close(); return
        ws_handshake(conn, raw)
        while True:
            data = conn.recv(4096)
            if not data: break
            msg = ws_decode(data)
            if not msg: break
            payload = json.loads(msg.decode("utf-8"))
            mtype = payload.get("type")
            if mtype == "join":
                code = payload.get("code"); client_code = code
                if code not in parties: parties[code] = []
                parties[code].append(conn)
                role = "host" if len(parties[code]) == 1 else "guest"
                ws_send(conn, json.dumps({"type": "joined", "role": role, "peers": len(parties[code])}))
                for c in parties[code]:
                    if c is not conn: ws_send(c, json.dumps({"type": "peer_joined", "peers": len(parties[code])}))
            elif mtype in ("offer", "answer", "candidate"):
                code = payload.get("code") or client_code
                if code and code in parties:
                    for c in parties[code]:
                        if c is not conn: ws_send(c, json.dumps(payload))
            elif mtype == "leave": break
    except: pass
    finally:
        if client_code and client_code in parties:
            if conn in parties[client_code]: parties[client_code].remove(conn)
            if not parties[client_code]: del parties[client_code]
        try: conn.close()
        except: pass

def run_ws_server():
    import socket
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        srv.bind((HOST, PORT_WS)); srv.listen(20)
        while True:
            conn, addr = srv.accept()
            threading.Thread(target=handle_ws_client, args=(conn, addr), daemon=True).start()
    except Exception as e: print(f"  ⚠️  WebSocket: {e}")

# ─── HTTP HANDLER avec endpoint /install ───
class ZGOSHandler(http.server.SimpleHTTPRequestHandler):
    def log_message(self, format, *args): pass

    def send_json(self, code, data):
        body = json.dumps(data).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", len(body))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(body)

    def do_OPTIONS(self):
        self.send_response(200)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "POST, GET, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()

    def do_POST(self):
        if self.path == "/install":
            length = int(self.headers.get("Content-Length", 0))
            body = json.loads(self.rfile.read(length).decode())
            game_name = body.get("name", "game")
            game_url  = body.get("url", "")
            if not game_url:
                self.send_json(400, {"error": "Missing URL"}); return
            try:
                # Fetch the game file from GitHub
                req = Request(game_url, headers={"User-Agent": "ZGOS/3.0"})
                with urlopen(req, timeout=15) as resp:
                    content = resp.read()
                # Save to installed_games/
                os.makedirs(GAMES_DIR, exist_ok=True)
                # Safe filename
                safe = "".join(c if c.isalnum() or c in "-_." else "_" for c in game_name)
                filename = f"{safe}.html"
                filepath = os.path.join(GAMES_DIR, filename)
                with open(filepath, "wb") as f:
                    f.write(content)
                local_url = f"http://{HOST}:{PORT_HTTP}/{GAMES_DIR}/{filename}"
                print(f"  ✓ Installé : {game_name} → {filename}")
                self.send_json(200, {"success": True, "local_url": local_url, "filename": filename})
            except Exception as e:
                print(f"  ❌ Erreur install {game_name}: {e}")
                self.send_json(500, {"error": str(e)})

        elif self.path == "/uninstall":
            length = int(self.headers.get("Content-Length", 0))
            body = json.loads(self.rfile.read(length).decode())
            filename = body.get("filename", "")
            filepath = os.path.join(GAMES_DIR, filename)
            if filename and os.path.exists(filepath):
                os.remove(filepath)
                print(f"  🗑️  Désinstallé : {filename}")
                self.send_json(200, {"success": True})
            else:
                self.send_json(404, {"error": "File not found"})
        else:
            self.send_json(404, {"error": "Not found"})

    def do_GET(self):
        if self.path == "/installed":
            # List installed games
            os.makedirs(GAMES_DIR, exist_ok=True)
            files = [f for f in os.listdir(GAMES_DIR) if f.endswith(".html")]
            self.send_json(200, {"files": files})
        else:
            super().do_GET()

def launch_browser():
    time.sleep(0.8)
    url = f"http://{HOST}:{PORT_HTTP}/index.html"
    print(f"\n  🌐 Ouverture → {url}\n")
    webbrowser.open(url)

def main():
    script_dir = os.path.dirname(os.path.abspath(__file__))
    os.chdir(script_dir)
    os.makedirs(GAMES_DIR, exist_ok=True)

    print("\n" + "="*45)
    print("  ███████╗ ██████╗  ██████╗ ███████╗")
    print("  ╚════██║██╔════╝ ██╔═══██╗██╔════╝")
    print("      ██╔╝██║  ███╗██║   ██║███████╗")
    print("     ██╔╝ ██║   ██║██║   ██║╚════██║")
    print("     ██║  ╚██████╔╝╚██████╔╝███████║")
    print("     ╚═╝   ╚═════╝  ╚═════╝ ╚══════╝")
    print("="*45)
    print(f"  HTTP  → http://{HOST}:{PORT_HTTP}")
    print(f"  WS    → ws://{HOST}:{PORT_WS}  (Party/Visio)")
    print(f"  Jeux  → ./{GAMES_DIR}/")
    print("  Ctrl+C pour arrêter")
    print("="*45)

    threading.Thread(target=run_ws_server, daemon=True).start()
    threading.Thread(target=launch_browser, daemon=True).start()

    try:
        with socketserver.TCPServer((HOST, PORT_HTTP), ZGOSHandler) as httpd:
            httpd.serve_forever()
    except OSError as e:
        if "Address already in use" in str(e):
            print(f"\n  ⚠️  Port {PORT_HTTP} déjà utilisé.")
            webbrowser.open(f"http://{HOST}:{PORT_HTTP}/index.html")
        else:
            print(f"\n  ❌ Erreur: {e}"); sys.exit(1)
    except KeyboardInterrupt:
        print("\n\n  👋 ZGOS arrêté.\n"); sys.exit(0)

if __name__ == "__main__":
    main()
