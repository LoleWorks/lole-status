"""Lole Status: one Telegram bot for the monitoring of every Lole Works client.

The bot lives in one Telegram group with Topics (the Lole Works team only): one topic per client, plus "General" for
the overview. Each client server runs its own monitor (e.g. bm-bi/monitor) that sends its alerts and a heartbeat with
its current state to this hub over HTTP, with its own key. The hub:
  * posts each client's alerts in that client's topic;
  * notices a client that went silent (no heartbeat for HUB_SILENT_MIN minutes: server, Docker or network down) and
    says so, and again when it is back;
  * answers commands: in a client topic /status, /surse, /server (the last state that client reported); in General
    /clienti (all clients at a glance); /ajutor everywhere;
  * posts a daily overview in General at HUB_DAILY_AT.
Only the bot holds the Telegram token; client servers only have their own key (revocable, stored hashed here).

HTTP API (on the internal Docker network, no public port by default):
  POST /api/message    {"text": "..."}                                     an alert to post in the client's topic
  POST /api/heartbeat  {"ok": bool, "problems": int, "status": "...", "sources": "...", "server": "..."}
  GET  /health                                                             for Docker
  Header: Authorization: Bearer <client key>

Command line (on the server):
  python hub.py serve                        run the hub (default)
  python hub.py add-client "BM Public"       create the client and its topic, print its key once
  python hub.py list-clients
  python hub.py rotate-key <id>              new key for a client (the old one stops working)
  python hub.py create-topic <id>            create the client's topic now (if it could not be created at add-client)
  python hub.py disable-client <id> / enable-client <id>
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import secrets
import sqlite3
import sys
import threading
import time
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import requests

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)-7s %(message)s")
log = logging.getLogger("lole_status")


def env(name: str, default: str = "") -> str:
    return os.environ.get(name, "").strip() or default


TOKEN = env("TELEGRAM_BOT_TOKEN")
GROUP = env("TELEGRAM_GROUP_ID")                 # the Lole Status group (a supergroup with Topics)
DB_PATH = env("HUB_DB", "/data/hub.db")
PORT = int(env("HUB_PORT", "8080"))
SILENT_MIN = int(env("HUB_SILENT_MIN", "15"))
DAILY_AT = env("HUB_DAILY_AT", "09:00")
MAX_BODY = 64 * 1024


# ---------------------------------------------------------------- storage
def db() -> sqlite3.Connection:
    c = sqlite3.connect(DB_PATH, timeout=10, isolation_level=None)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA journal_mode=WAL")
    c.executescript("""
        CREATE TABLE IF NOT EXISTS clients (
            id          INTEGER PRIMARY KEY,
            name        TEXT NOT NULL UNIQUE,
            key_hash    TEXT NOT NULL UNIQUE,
            topic_id    INTEGER,
            active      INTEGER NOT NULL DEFAULT 1,
            created_at  REAL NOT NULL,
            last_seen   REAL,
            silent      INTEGER NOT NULL DEFAULT 0,     -- 1 = "went silent" already announced
            snapshot    TEXT                            -- last heartbeat (JSON)
        );
        CREATE TABLE IF NOT EXISTS events (
            id         INTEGER PRIMARY KEY,
            client_id  INTEGER,
            at         REAL NOT NULL,
            kind       TEXT NOT NULL,
            text       TEXT
        );
        CREATE TABLE IF NOT EXISTS kv (k TEXT PRIMARY KEY, v TEXT);
    """)
    return c


def key_hash(key: str) -> str:
    return hashlib.sha256(key.encode()).hexdigest()


def kv_get(c, k):
    r = c.execute("SELECT v FROM kv WHERE k = ?", (k,)).fetchone()
    return r["v"] if r else None


def kv_set(c, k, v):
    c.execute("INSERT INTO kv (k, v) VALUES (?, ?) ON CONFLICT(k) DO UPDATE SET v = excluded.v", (k, v))


# ---------------------------------------------------------------- Telegram
def tg(method: str, **params):
    if not TOKEN:
        log.warning("TELEGRAM_BOT_TOKEN is not set (%s skipped)", method)
        return None
    try:
        r = requests.post(f"https://api.telegram.org/bot{TOKEN}/{method}", json=params, timeout=40)
        data = r.json()
        if not data.get("ok"):
            log.warning("telegram %s: %s", method, data.get("description"))
        return data
    except Exception as e:  # noqa: BLE001
        log.warning("telegram %s: %s", method, str(e).replace(TOKEN, "***")[:200])
        return None


def post(text: str, topic: int | None = None, chat: str | None = None) -> None:
    chat = chat or GROUP
    if not chat:
        log.info("(no TELEGRAM_GROUP_ID) %s", text.splitlines()[0][:120])
        return
    for i in range(0, len(text), 3900):
        params = {"chat_id": chat, "text": text[i:i + 3900], "disable_web_page_preview": True}
        if topic:
            params["message_thread_id"] = topic
        tg("sendMessage", **params)


def create_topic(name: str) -> tuple[int | None, str]:
    """Creates a forum topic in the group; returns (topic id, error text)."""
    if not GROUP:
        return None, "TELEGRAM_GROUP_ID nu este setat"
    res = tg("createForumTopic", chat_id=GROUP, name=name)
    if res and res.get("ok"):
        return res["result"]["message_thread_id"], ""
    return None, (res or {}).get("description", "fără răspuns de la Telegram")


def ensure_topic(c, cl) -> int | None:
    """A client without a topic (creation failed earlier) gets one as soon as it is possible; tried at most every 10 min."""
    if cl["topic_id"] or not GROUP:
        return cl["topic_id"]
    k = f"topic_try_{cl['id']}"
    if time.time() - float(kv_get(c, k) or 0) < 600:
        return None
    kv_set(c, k, str(time.time()))
    topic, err = create_topic(cl["name"])
    if topic:
        c.execute("UPDATE clients SET topic_id = ? WHERE id = ?", (topic, cl["id"]))
        post(f"Topicul clientului {cl['name']}. Aici apar alertele serverului lui. /ajutor pentru comenzi.", topic)
        log.info("topic %s created for %s", topic, cl["name"])
    else:
        log.warning("cannot create the topic for %s: %s", cl["name"], err)
    return topic


def ago(ts: float | None) -> str:
    if not ts:
        return "niciodată"
    m = int((time.time() - ts) // 60)
    return "acum" if m < 1 else f"acum {m} min" if m < 60 else f"acum {m // 60} h {m % 60} min" if m < 1440 else f"acum {m // 1440} zile"


# ---------------------------------------------------------------- HTTP API
class Api(BaseHTTPRequestHandler):
    server_version = "LoleStatus"

    def log_message(self, fmt, *args):  # quiet
        pass

    def _send(self, code: int, body: dict) -> None:
        b = json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)

    def do_GET(self):
        if self.path == "/health":
            self._send(200, {"ok": True})
        else:
            self._send(404, {"error": "not found"})

    def do_POST(self):
        auth = self.headers.get("Authorization", "")
        key = auth[7:].strip() if auth.startswith("Bearer ") else ""
        length = int(self.headers.get("Content-Length") or 0)
        if not key or length > MAX_BODY:
            return self._send(401 if not key else 413, {"error": "unauthorized" if not key else "too large"})
        try:
            body = json.loads(self.rfile.read(length) or b"{}")
        except ValueError:
            return self._send(400, {"error": "invalid json"})
        c = db()
        try:
            cl = c.execute("SELECT * FROM clients WHERE key_hash = ? AND active = 1", (key_hash(key),)).fetchone()
            if not cl:
                return self._send(401, {"error": "unknown key"})
            now = time.time()
            if cl["silent"]:   # it was silent and talks again
                c.execute("UPDATE clients SET silent = 0 WHERE id = ?", (cl["id"],))
                post(f"[REVENIT] {cl['name']}: monitorul trimite din nou date (a tăcut de la "
                     f"{datetime.fromtimestamp(cl['last_seen'] or now):%d.%m %H:%M}).", cl["topic_id"] or None)
            c.execute("UPDATE clients SET last_seen = ? WHERE id = ?", (now, cl["id"]))
            topic = ensure_topic(c, cl)
            if self.path == "/api/message":
                text = str(body.get("text") or "").strip()[:12000]
                if text:
                    post(text if topic else f"[{cl['name']}] {text}", topic)
                    c.execute("INSERT INTO events (client_id, at, kind, text) VALUES (?, ?, 'message', ?)", (cl["id"], now, text))
                return self._send(200, {"ok": True})
            if self.path == "/api/heartbeat":
                snap = {k: body.get(k) for k in ("ok", "problems", "status", "sources", "server")}
                snap["at"] = now
                c.execute("UPDATE clients SET snapshot = ? WHERE id = ?", (json.dumps(snap)[:MAX_BODY], cl["id"]))
                return self._send(200, {"ok": True})
            return self._send(404, {"error": "not found"})
        finally:
            c.close()


# ---------------------------------------------------------------- background: silence, daily overview, commands
def overview(c) -> str:
    rows = c.execute("SELECT * FROM clients WHERE active = 1 ORDER BY name").fetchall()
    if not rows:
        return "Niciun client încă. Pe server: python hub.py add-client \"Nume client\""
    lines = ["Clienți monitorizați:"]
    for r in rows:
        snap = json.loads(r["snapshot"]) if r["snapshot"] else {}
        state = ("TĂCUT (fără semnal)" if r["silent"] else
                 "OK" if snap.get("ok") else f"{snap.get('problems', '?')} probleme" if snap else "fără date încă")
        lines.append(f"- {r['name']}: {state}, ultimul semnal {ago(r['last_seen'])}")
    return "\n".join(lines)


def watch_loop() -> None:
    while True:
        try:
            c = db()
            now = time.time()
            for r in c.execute("SELECT * FROM clients WHERE active = 1 AND silent = 0 AND last_seen IS NOT NULL").fetchall():
                if now - r["last_seen"] > SILENT_MIN * 60:
                    c.execute("UPDATE clients SET silent = 1 WHERE id = ?", (r["id"],))
                    post(f"[TĂCERE] {r['name']}: niciun semnal de la monitor de {ago(r['last_seen'])[5:]}.\n"
                         "Serverul, Docker-ul, containerul monitor sau rețeaua către hub sunt căzute.\n"
                         "Ce faci: verifică serverul (ping/SSH), apoi docker ps și docker logs pentru containerul monitor.", r["topic_id"])
            hh, mm = (int(x) for x in DAILY_AT.split(":"))
            today = datetime.now().strftime("%Y-%m-%d")
            if datetime.now() >= datetime.now().replace(hour=hh, minute=mm, second=0) and kv_get(c, "daily") != today:
                kv_set(c, "daily", today)
                post(f"Raport zilnic Lole Status\n{overview(c)}")
            c.close()
        except Exception as e:  # noqa: BLE001
            log.exception("watch: %s", e)
        time.sleep(60)


HELP_TOPIC = "Comenzi în topicul unui client:\n/status - problemele deschise\n/surse - încărcările de date\n/server - disc, memorie, containere\n/ajutor"
HELP_GENERAL = "Comenzi în General:\n/clienti - toți clienții dintr-o privire\n/ajutor\nÎn topicul fiecărui client: /status, /surse, /server."


def bot_loop() -> None:
    offset = None
    while True:
        res = tg("getUpdates", timeout=30, offset=offset, allowed_updates=["message"])
        if not res or not res.get("ok"):
            time.sleep(10)
            continue
        for u in res.get("result", []):
            offset = u["update_id"] + 1
            msg = u.get("message") or {}
            chat = str((msg.get("chat") or {}).get("id", ""))
            text = (msg.get("text") or "").strip()
            if not text.startswith("/"):
                continue
            cmd = text.split()[0].split("@")[0].lower()
            if chat != GROUP:
                # private chats and other groups: only tell the id, so the group can be configured
                post(f"Lole Status răspunde doar în grupul configurat. ID-ul acestui chat: {chat}", chat=chat)
                log.info("command from chat %s (not TELEGRAM_GROUP_ID)", chat)
                continue
            topic = msg.get("message_thread_id") if msg.get("is_topic_message") else None
            c = db()
            try:
                cl = c.execute("SELECT * FROM clients WHERE topic_id = ?", (topic,)).fetchone() if topic else None
                if not cl:
                    post(overview(c) if cmd in ("/clienti", "/status", "/start") else HELP_GENERAL, topic)
                    continue
                snap = json.loads(cl["snapshot"]) if cl["snapshot"] else None
                if not snap:
                    post(f"{cl['name']}: nu am primit încă date de la monitor.", topic)
                    continue
                stamp = f"(raportat {ago(snap.get('at'))})"
                part = {"/status": "status", "/start": "status", "/surse": "sources", "/server": "server"}.get(cmd)
                if part:
                    silent = "\nATENȚIE: monitorul tace, datele pot fi vechi." if cl["silent"] else ""
                    post(f"{snap.get(part) or 'nimic raportat'}\n{stamp}{silent}", topic)
                else:
                    post(HELP_TOPIC, topic)
            finally:
                c.close()


# ---------------------------------------------------------------- command line
def add_client(name: str) -> None:
    c = db()
    key = "ls_" + secrets.token_urlsafe(32)
    topic, err = create_topic(name)
    if not topic:
        print(f"ATENȚIE: topicul nu a putut fi creat: {err}")
        print("Verifică: grupul are Topics activate, botul e administrator cu dreptul „Gestionare topicuri”, "
              "TELEGRAM_GROUP_ID e ID-ul grupului. Topicul se creează singur la următorul mesaj, după ce rezolvi.")
    c.execute("INSERT INTO clients (name, key_hash, topic_id, created_at) VALUES (?, ?, ?, ?)", (name, key_hash(key), topic, time.time()))
    if topic:
        post(f"Topicul clientului {name}. Aici apar alertele serverului lui. /ajutor pentru comenzi.", topic)
    print(f"Client creat: {name} (topic {topic})")
    print(f"Cheia lui (se afișează o singură dată, pune-o în STATUS_HUB_KEY pe serverul clientului):\n{key}")


def main(argv: list[str]) -> int:
    cmd = argv[1] if len(argv) > 1 else "serve"
    if cmd == "add-client" and len(argv) == 3:
        add_client(argv[2]); return 0
    if cmd == "list-clients":
        c = db()
        for r in c.execute("SELECT id, name, topic_id, active, last_seen, silent FROM clients ORDER BY id"):
            print(f"{r['id']:>3}  {r['name']:<30} topic {r['topic_id']}  {'activ' if r['active'] else 'dezactivat'}"
                  f"  ultimul semnal {ago(r['last_seen'])}{'  TĂCUT' if r['silent'] else ''}")
        return 0
    if cmd == "rotate-key" and len(argv) == 3:
        key = "ls_" + secrets.token_urlsafe(32)
        n = db().execute("UPDATE clients SET key_hash = ? WHERE id = ?", (key_hash(key), int(argv[2]))).rowcount
        print(f"Cheie nouă:\n{key}" if n else "Client inexistent"); return 0
    if cmd == "create-topic" and len(argv) == 3:
        c = db()
        cl = c.execute("SELECT * FROM clients WHERE id = ?", (int(argv[2]),)).fetchone()
        if not cl:
            print("Client inexistent"); return 1
        topic, err = create_topic(cl["name"])
        if not topic:
            print(f"Topicul nu a putut fi creat: {err}"); return 1
        c.execute("UPDATE clients SET topic_id = ? WHERE id = ?", (topic, cl["id"]))
        post(f"Topicul clientului {cl['name']}. Aici apar alertele serverului lui. /ajutor pentru comenzi.", topic)
        print(f"Topic creat pentru {cl['name']}: {topic}"); return 0
    if cmd in ("disable-client", "enable-client") and len(argv) == 3:
        n = db().execute("UPDATE clients SET active = ? WHERE id = ?", (1 if cmd == "enable-client" else 0, int(argv[2]))).rowcount
        print("Gata" if n else "Client inexistent"); return 0
    if cmd != "serve":
        print(__doc__); return 2
    if not TOKEN or not GROUP:
        log.warning("TELEGRAM_BOT_TOKEN or TELEGRAM_GROUP_ID is not set: messages are only logged")
    db().close()
    threading.Thread(target=watch_loop, daemon=True).start()
    if TOKEN:
        threading.Thread(target=bot_loop, daemon=True).start()
    log.info("Lole Status hub on port %d", PORT)
    ThreadingHTTPServer(("0.0.0.0", PORT), Api).serve_forever()
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
