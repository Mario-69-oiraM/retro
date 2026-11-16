#!/usr/bin/env python3
"""
Retro Board — a self-contained, anonymous team retrospective board.

The facilitator runs this one file (Python 3, standard library only).
Everyone else opens the printed URL in a browser. Cards update live and
are saved to a JSON file next to this script.

    python3 retro.py                     # port 8080, facilitator passcode 12345
    python3 retro.py --port 9000
    python3 retro.py --data /path/to/board.json

Anonymity: the server never records who posted, voted or edited a card.
No names, IP addresses or timestamps are stored or logged.
"""

import argparse
import csv
import datetime
import hashlib
import io
import json
import os
import re
import secrets
import socket
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

MAX_CARD_LEN = 1000
MAX_TITLE_LEN = 80
MAX_COLUMNS = 8
MAX_BODY = 64 * 1024
LONG_POLL_SECONDS = 25

DEFAULT_COLUMNS = [
    {"id": "well", "title": "What went well", "color": "well"},
    {"id": "notwell", "title": "What didn't go well", "color": "notwell"},
    {"id": "actions", "title": "Actions", "color": "actions"},
]
# Colour keys the page knows about; new columns take the first one not in use.
PALETTE = ["well", "notwell", "actions", "purple", "amber", "teal", "pink", "slate"]


def _sha(s):
    return hashlib.sha256(s.encode("utf-8")).hexdigest()


def _new_id():
    return secrets.token_hex(8)


class Store:
    """Thread-safe board state persisted to a JSON file."""

    def __init__(self, path):
        self.path = path
        self.cond = threading.Condition(threading.Lock())
        self.data = self._load()

    def _empty(self):
        return {
            "version": 1,
            "boardId": _new_id(),
            "title": "Team Retro",
            "hidden": True,
            "columns": [dict(c) for c in DEFAULT_COLUMNS],
            "cards": [],
        }

    def _load(self):
        if not os.path.exists(self.path):
            return self._empty()
        try:
            with open(self.path, "r", encoding="utf-8") as f:
                data = json.load(f)
            base = self._empty()
            base.update({k: data[k] for k in base if k in data})
            return base
        except (OSError, ValueError) as e:
            stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
            backup = f"{self.path}.corrupt-{stamp}"
            try:
                os.replace(self.path, backup)
                print(f"! Could not read {self.path} ({e}); moved it to {backup}")
            except OSError:
                pass
            return self._empty()

    def _save(self):
        tmp = f"{self.path}.tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(self.data, f, ensure_ascii=False, indent=2)
        os.replace(tmp, self.path)

    def _commit(self):
        """Call with the lock held after any mutation."""
        self.data["version"] += 1
        self._save()
        self.cond.notify_all()

    # ---- reads -------------------------------------------------------------
    def public(self):
        d = self.data
        hidden = d["hidden"]
        return {
            "version": d["version"],
            "boardId": d["boardId"],
            "title": d["title"],
            "hidden": hidden,
            "columns": d["columns"],
            "cards": [
                {
                    "id": c["id"],
                    "column": c["column"],
                    "text": None if hidden else c["text"],
                    "votes": len(c["votes"]),
                }
                for c in d["cards"]
            ],
        }

    def wait_for_change(self, since):
        with self.cond:
            if self.data["version"] == since:
                self.cond.wait(LONG_POLL_SECONDS)
            return self.public()

    # ---- helpers -----------------------------------------------------------
    def _card(self, card_id):
        for c in self.data["cards"]:
            if c["id"] == card_id:
                return c
        raise ApiError(404, "Card not found (it may have been deleted).")

    def _column_ids(self):
        return {c["id"] for c in self.data["columns"]}

    # ---- card operations ---------------------------------------------------
    def add_card(self, column, text):
        text = clean_text(text, MAX_CARD_LEN, "Card")
        with self.cond:
            if column not in self._column_ids():
                raise ApiError(400, "Unknown column.")
            key = secrets.token_hex(16)
            card = {"id": _new_id(), "column": column, "text": text,
                    "votes": [], "keyHash": _sha(key)}
            self.data["cards"].append(card)
            self._commit()
            return {"id": card["id"], "key": key, "boardId": self.data["boardId"]}

    def edit_card(self, card_id, key, text):
        text = clean_text(text, MAX_CARD_LEN, "Card")
        with self.cond:
            card = self._card(card_id)
            if not secrets.compare_digest(card["keyHash"], _sha(str(key or ""))):
                raise ApiError(403, "You can only edit your own cards.")
            card["text"] = text
            self._commit()
            return {"ok": True}

    def delete_card(self, card_id, key, is_admin):
        with self.cond:
            card = self._card(card_id)
            owner = secrets.compare_digest(card["keyHash"], _sha(str(key or "")))
            if not (owner or is_admin):
                raise ApiError(403, "You can only delete your own cards.")
            self.data["cards"].remove(card)
            self._commit()
            return {"ok": True}

    def vote(self, card_id, voter):
        voter = str(voter or "")
        if len(voter) < 16:
            raise ApiError(400, "Missing voter token.")
        with self.cond:
            if self.data["hidden"]:
                raise ApiError(409, "Voting opens once cards are revealed.")
            card = self._card(card_id)
            # Hash is per-card, so votes can't be linked across cards.
            h = _sha(f"{voter}:{card_id}")
            if h in card["votes"]:
                card["votes"].remove(h)
                voted = False
            else:
                card["votes"].append(h)
                voted = True
            self._commit()
            return {"voted": voted}

    # ---- facilitator operations -------------------------------------------
    def rename_column(self, column, title):
        title = clean_text(title, MAX_TITLE_LEN, "Column name")
        with self.cond:
            for c in self.data["columns"]:
                if c["id"] == column:
                    c["title"] = title
                    self._commit()
                    return {"ok": True}
            raise ApiError(404, "Unknown column.")

    def add_column(self, title):
        title = clean_text(title, MAX_TITLE_LEN, "Column name")
        with self.cond:
            cols = self.data["columns"]
            if len(cols) >= MAX_COLUMNS:
                raise ApiError(400, f"A board can have at most {MAX_COLUMNS} columns.")
            used = {c.get("color", c["id"]) for c in cols}
            color = next((k for k in PALETTE if k not in used), PALETTE[len(cols) % len(PALETTE)])
            col = {"id": _new_id(), "title": title, "color": color}
            cols.append(col)
            self._commit()
            return {"ok": True, "id": col["id"]}

    def delete_column(self, column):
        with self.cond:
            cols = self.data["columns"]
            if column not in self._column_ids():
                raise ApiError(404, "Unknown column.")
            if len(cols) <= 1:
                raise ApiError(400, "A board needs at least one column.")
            self.data["columns"] = [c for c in cols if c["id"] != column]
            self.data["cards"] = [c for c in self.data["cards"] if c["column"] != column]
            self._commit()
            return {"ok": True}

    def rename_board(self, title):
        title = clean_text(title, MAX_TITLE_LEN, "Board title")
        with self.cond:
            self.data["title"] = title
            self._commit()
            return {"ok": True}

    def set_hidden(self, hidden):
        with self.cond:
            self.data["hidden"] = bool(hidden)
            self._commit()
            return {"ok": True}

    def clear(self):
        with self.cond:
            archived = None
            if self.data["cards"]:
                stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
                folder = os.path.dirname(os.path.abspath(self.path))
                archived = os.path.join(folder, f"retro-archive-{stamp}.json")
                with open(archived, "w", encoding="utf-8") as f:
                    json.dump(export_data(self.data), f, ensure_ascii=False, indent=2)
            self.data["cards"] = []
            self.data["boardId"] = _new_id()
            self.data["hidden"] = True
            self._commit()
            return {"ok": True, "archived": os.path.basename(archived) if archived else None}

    def snapshot(self):
        with self.cond:
            return json.loads(json.dumps(self.data))


class ApiError(Exception):
    def __init__(self, status, message):
        super().__init__(message)
        self.status = status
        self.message = message


def clean_text(value, limit, label):
    text = str(value or "").strip()
    if not text:
        raise ApiError(400, f"{label} can't be empty.")
    if len(text) > limit:
        raise ApiError(400, f"{label} is too long (max {limit} characters).")
    return text


# ---- export ------------------------------------------------------------------

def export_data(data):
    """Card data without any internal hashes, sorted by votes within column."""
    out = {
        "title": data["title"],
        "exported": datetime.datetime.now().strftime("%Y-%m-%d %H:%M"),
        "columns": [],
    }
    for col in data["columns"]:
        cards = [c for c in data["cards"] if c["column"] == col["id"]]
        cards.sort(key=lambda c: -len(c["votes"]))
        out["columns"].append({
            "title": col["title"],
            "cards": [{"text": c["text"], "votes": len(c["votes"])} for c in cards],
        })
    return out


def export_markdown(data):
    e = export_data(data)
    lines = [f"# {e['title']}", "", f"_Exported {e['exported']}_", ""]
    for col in e["columns"]:
        lines.append(f"## {col['title']} ({len(col['cards'])})")
        lines.append("")
        if not col["cards"]:
            lines.append("_No cards_")
        for c in col["cards"]:
            text = (c["text"].replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
                    .replace("\r\n", "\n").replace("\n", "<br>"))
            votes = f" **(+{c['votes']})**" if c["votes"] else ""
            lines.append(f"- {text}{votes}")
        lines.append("")
    return "\n".join(lines)


def export_csv(data):
    e = export_data(data)
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["Column", "Card", "Votes"])
    for col in e["columns"]:
        for c in col["cards"]:
            w.writerow([col["title"], c["text"], c["votes"]])
    return "﻿" + buf.getvalue()  # BOM so Excel reads UTF-8 correctly


def slug(s):
    s = re.sub(r"[^A-Za-z0-9]+", "-", s).strip("-").lower()
    return s or "retro"


# ---- HTTP ----------------------------------------------------------------------

class Handler(BaseHTTPRequestHandler):
    server_version = "RetroBoard/1.0"
    store = None      # set in main()
    passcode = ""     # set in main()

    def log_message(self, fmt, *args):
        # Deliberately silent: no IP addresses or request logs, for anonymity.
        pass

    # -- helpers
    def _send(self, status, body, ctype, extra=None):
        data = body.encode("utf-8") if isinstance(body, str) else body
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(data)

    def _json(self, status, obj):
        self._send(status, json.dumps(obj, ensure_ascii=False), "application/json; charset=utf-8")

    def _body(self):
        length = int(self.headers.get("Content-Length") or 0)
        if length > MAX_BODY:
            raise ApiError(413, "Request too large.")
        raw = self.rfile.read(length) if length else b"{}"
        try:
            body = json.loads(raw.decode("utf-8") or "{}")
        except ValueError:
            raise ApiError(400, "Invalid JSON.")
        if not isinstance(body, dict):
            raise ApiError(400, "Invalid request.")
        return body

    def _is_admin(self):
        given = self.headers.get("X-Facilitator", "")
        return secrets.compare_digest(given.encode("utf-8"), self.passcode.encode("utf-8"))

    def _require_admin(self):
        if not self._is_admin():
            raise ApiError(403, "Facilitator passcode required.")

    # -- routes
    def do_GET(self):
        try:
            url = urlparse(self.path)
            q = parse_qs(url.query)
            if url.path in ("/", "/index.html"):
                self._send(200, PAGE, "text/html; charset=utf-8",
                           {"Content-Security-Policy":
                            "default-src 'self'; style-src 'self' 'unsafe-inline'; "
                            "script-src 'self' 'unsafe-inline'; img-src 'self' data:"})
            elif url.path == "/api/state":
                try:
                    since = int(q.get("v", ["0"])[0])
                except ValueError:
                    since = 0
                self._json(200, self.store.wait_for_change(since))
            elif url.path == "/api/export":
                self._require_admin()
                fmt = q.get("format", ["md"])[0]
                data = self.store.snapshot()
                name = f"{slug(data['title'])}-{datetime.date.today().isoformat()}"
                if fmt == "csv":
                    body, ctype, ext = export_csv(data), "text/csv; charset=utf-8", "csv"
                elif fmt == "json":
                    body = json.dumps(export_data(data), ensure_ascii=False, indent=2)
                    ctype, ext = "application/json; charset=utf-8", "json"
                else:
                    body, ctype, ext = export_markdown(data), "text/markdown; charset=utf-8", "md"
                self._send(200, body, ctype,
                           {"Content-Disposition": f'attachment; filename="{name}.{ext}"'})
            elif url.path == "/favicon.ico":
                self._send(204, b"", "image/x-icon")
            else:
                self._json(404, {"error": "Not found"})
        except ApiError as e:
            self._json(e.status, {"error": e.message})
        except (BrokenPipeError, ConnectionResetError):
            pass

    def do_POST(self):
        s = self.store
        try:
            path = urlparse(self.path).path
            b = self._body()
            if path == "/api/cards/add":
                r = s.add_card(b.get("column"), b.get("text"))
            elif path == "/api/cards/edit":
                r = s.edit_card(b.get("id"), b.get("key"), b.get("text"))
            elif path == "/api/cards/delete":
                r = s.delete_card(b.get("id"), b.get("key"), self._is_admin())
            elif path == "/api/vote":
                r = s.vote(b.get("id"), b.get("voter"))
            elif path == "/api/admin/check":
                self._require_admin()
                r = {"ok": True}
            elif path == "/api/admin/rename-column":
                self._require_admin()
                r = s.rename_column(b.get("column"), b.get("title"))
            elif path == "/api/admin/add-column":
                self._require_admin()
                r = s.add_column(b.get("title"))
            elif path == "/api/admin/delete-column":
                self._require_admin()
                r = s.delete_column(b.get("column"))
            elif path == "/api/admin/rename-board":
                self._require_admin()
                r = s.rename_board(b.get("title"))
            elif path == "/api/admin/hidden":
                self._require_admin()
                r = s.set_hidden(b.get("hidden"))
            elif path == "/api/admin/clear":
                self._require_admin()
                r = s.clear()
            else:
                raise ApiError(404, "Not found")
            self._json(200, r)
        except ApiError as e:
            self._json(e.status, {"error": e.message})
        except (BrokenPipeError, ConnectionResetError):
            pass


def lan_ip():
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.connect(("10.255.255.255", 1))
        ip = sock.getsockname()[0]
        sock.close()
        return ip
    except OSError:
        return "127.0.0.1"


def main():
    here = os.path.dirname(os.path.abspath(__file__))
    ap = argparse.ArgumentParser(description="Anonymous retro board server.")
    ap.add_argument("--port", type=int, default=8080, help="port to listen on (default 8080)")
    ap.add_argument("--host", default="0.0.0.0", help="interface to bind (default: all)")
    ap.add_argument("--data", default=os.path.join(here, "retro-data.json"),
                    help="board data file (default: retro-data.json next to this script)")
    args = ap.parse_args()

    Handler.store = Store(os.path.abspath(args.data))
    Handler.passcode = "12345"

    try:
        httpd = ThreadingHTTPServer((args.host, args.port), Handler)
    except OSError as e:
        sys.exit(f"Could not start on port {args.port}: {e}\n"
                 f"Try another port, e.g.  python3 retro.py --port 8090")
    httpd.daemon_threads = True

    ip = lan_ip()
    host = socket.gethostname()
    print("=" * 60)
    print("  Retro Board is running")
    print("=" * 60)
    print(f"  Share this link with the team:  http://{ip}:{args.port}")
    print(f"  (or by machine name:            http://{host}:{args.port})")
    print(f"  On this computer:               http://localhost:{args.port}")
    print()
    print(f"  Facilitator passcode:  {Handler.passcode}")
    print(f"  Data file:             {Handler.store.path}")
    print()
    print("  Press Ctrl+C to stop. The board is saved after every change.")
    print("=" * 60)
    sys.stdout.flush()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped. Board data is saved.")
    finally:
        httpd.server_close()


# ---- the web page --------------------------------------------------------------

PAGE = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Retro Board</title>
<link rel="icon" href="data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 32 32'%3E%3Crect x='2' y='5' width='8' height='22' rx='2' fill='%2322a06b'/%3E%3Crect x='12' y='5' width='8' height='16' rx='2' fill='%23e0566a'/%3E%3Crect x='22' y='5' width='8' height='19' rx='2' fill='%233f7fe0'/%3E%3C/svg%3E">
<style>
:root{
  --bg:#f5f4f0; --surface:#ffffff; --surface-2:#faf9f6; --text:#1f2328; --muted:#6a6f76;
  --line:#e3e1da; --shadow:0 1px 2px rgba(20,20,10,.06),0 2px 8px rgba(20,20,10,.05);
  --well:#1f9d68; --well-bg:#e8f6ef; --notwell:#d64b5f; --notwell-bg:#fcebee;
  --actions:#3a78dc; --actions-bg:#e9f0fc; --accent:#3a78dc; --danger:#c8364b;
  --purple:#7d55c7; --purple-bg:#f1ecfa; --amber:#b7791f; --amber-bg:#fbf3e4;
  --teal:#13908f; --teal-bg:#e4f5f4; --pink:#c2408f; --pink-bg:#fbeaf4;
  --slate:#5b6573; --slate-bg:#eef0f3;
  --focus:0 0 0 3px rgba(58,120,220,.3);
}
@media (prefers-color-scheme: dark){
  :root{
    --bg:#16181c; --surface:#1f2228; --surface-2:#252930; --text:#e8e9eb; --muted:#9aa0a8;
    --line:#30343c; --shadow:0 1px 2px rgba(0,0,0,.4);
    --well:#3cc48a; --well-bg:#18302a; --notwell:#f0707f; --notwell-bg:#3a1f25;
    --actions:#6a9ef0; --actions-bg:#1c2a40; --accent:#6a9ef0; --danger:#f0707f;
    --purple:#a98bf0; --purple-bg:#2a2340; --amber:#e0a84a; --amber-bg:#352a17;
    --teal:#3cc4c0; --teal-bg:#163232; --pink:#ee7fc0; --pink-bg:#3a1f30;
    --slate:#a3acb9; --slate-bg:#272b32;
  }
}
*{box-sizing:border-box}
[hidden]{display:none!important}
html,body{margin:0}
body{background:var(--bg);color:var(--text);font:15px/1.45 system-ui,-apple-system,"Segoe UI",Roboto,sans-serif;min-height:100vh}
button,input,textarea{font:inherit;color:inherit}
button{cursor:pointer}
:focus-visible{outline:none;box-shadow:var(--focus)}

header{display:flex;flex-wrap:wrap;align-items:center;gap:12px 16px;padding:14px 20px;border-bottom:1px solid var(--line);background:var(--surface)}
.brand{display:flex;align-items:center;gap:10px;min-width:0;flex:1 1 240px}
.brand h1{font-size:19px;margin:0;font-weight:650;letter-spacing:-.01em;overflow-wrap:anywhere}
.anon{font-size:12px;color:var(--muted);border:1px solid var(--line);border-radius:999px;padding:2px 9px;white-space:nowrap}
.tools{display:flex;flex-wrap:wrap;align-items:center;gap:8px}
.conn{width:8px;height:8px;border-radius:50%;background:var(--well);display:inline-block}
.conn.off{background:var(--danger)}
.btn{border:1px solid var(--line);background:var(--surface);border-radius:8px;padding:6px 12px;font-size:14px;display:inline-flex;align-items:center;gap:6px}
.btn:hover{background:var(--surface-2)}
.btn.primary{background:var(--accent);border-color:var(--accent);color:#fff}
.btn.primary:hover{filter:brightness(1.05)}
.btn.danger{color:var(--danger)}
.btn:disabled{opacity:.5;cursor:default}
.toggle{display:inline-flex;align-items:center;gap:6px;font-size:14px;color:var(--muted);user-select:none}
.icon{border:0;background:none;padding:4px;border-radius:6px;color:var(--muted);line-height:0}
.icon:hover{background:var(--surface-2);color:var(--text)}
.icon svg{width:16px;height:16px}

.adminbar{display:none;flex-wrap:wrap;align-items:center;gap:8px;padding:10px 20px;background:var(--surface-2);border-bottom:1px solid var(--line);font-size:14px}
.adminbar.on{display:flex}
.adminbar .label{font-weight:600;margin-right:4px}
.adminbar .sep{width:1px;height:22px;background:var(--line);margin:0 4px}

.banner{margin:16px 20px 0;padding:10px 14px;border-radius:10px;background:var(--surface);border:1px dashed var(--line);color:var(--muted);font-size:14px;display:none}
.banner.on{display:block}

main{display:grid;grid-template-columns:repeat(auto-fit,minmax(260px,1fr));gap:16px;padding:16px 20px 40px;align-items:start}
@media (max-width:600px){header,.adminbar{padding-left:16px;padding-right:16px} main{padding:16px} .banner{margin:16px 16px 0}}

.col{background:var(--surface);border:1px solid var(--line);border-radius:14px;box-shadow:var(--shadow);overflow:hidden;display:flex;flex-direction:column}
.col-head{padding:14px 14px 10px;border-top:4px solid var(--c)}
.col-title{display:flex;align-items:center;gap:6px}
.col-title h2{font-size:16px;margin:0;font-weight:650;flex:1;overflow-wrap:anywhere}
.count{font-size:12px;font-weight:600;color:var(--c);background:var(--cbg);border-radius:999px;padding:1px 8px}
.rename{display:flex;gap:6px;flex:1}
.rename input{flex:1;min-width:0;border:1px solid var(--line);border-radius:8px;padding:4px 8px;background:var(--surface)}
.add{margin-top:10px;display:flex;flex-direction:column;gap:6px}
.add textarea{width:100%;resize:vertical;min-height:62px;border:1px solid var(--line);border-radius:10px;padding:8px 10px;background:var(--surface-2)}
.add textarea:focus{background:var(--surface);border-color:var(--c)}
.add-row{display:flex;justify-content:space-between;align-items:center;gap:8px;font-size:12px;color:var(--muted)}
.add-row .btn{background:var(--c);border-color:var(--c);color:#fff;padding:5px 12px}
.add-row .btn:hover{filter:brightness(1.05);background:var(--c)}

.cards{list-style:none;margin:0;padding:4px 14px 14px;display:flex;flex-direction:column;gap:8px}
.empty{color:var(--muted);font-size:13px;text-align:center;padding:14px 0 6px}
.card{background:var(--cbg);border-radius:10px;padding:10px 12px 8px;border:1px solid transparent}
.card.mine{border-color:color-mix(in srgb,var(--c) 45%,transparent)}
.card .text{margin:0;white-space:pre-wrap;overflow-wrap:anywhere}
.card.masked .text{filter:blur(5px);user-select:none;color:var(--muted)}
.meta{display:flex;align-items:center;gap:4px;margin-top:6px;min-height:26px}
.tag{font-size:11px;color:var(--c);font-weight:600;margin-right:auto}
.spacer{margin-right:auto}
.vote{border:1px solid var(--line);background:var(--surface);border-radius:999px;padding:2px 9px;font-size:13px;display:inline-flex;align-items:center;gap:4px;color:var(--muted)}
.vote svg{width:12px;height:12px}
.vote.on{background:var(--c);border-color:var(--c);color:#fff}
.vote:disabled{opacity:.45;cursor:default}
.edit-area{width:100%;min-height:62px;resize:vertical;border:1px solid var(--c);border-radius:8px;padding:6px 8px;background:var(--surface)}
.edit-actions{display:flex;gap:6px;justify-content:flex-end;margin-top:6px}

.toast{position:fixed;left:50%;bottom:20px;transform:translateX(-50%) translateY(20px);background:var(--text);color:var(--bg);padding:9px 16px;border-radius:10px;font-size:14px;opacity:0;transition:.2s;pointer-events:none;max-width:calc(100% - 32px)}
.toast.on{opacity:1;transform:translateX(-50%)}

dialog{border:1px solid var(--line);border-radius:14px;padding:20px;background:var(--surface);color:var(--text);width:min(400px,calc(100% - 32px));box-shadow:0 10px 40px rgba(0,0,0,.25)}
dialog::backdrop{background:rgba(0,0,0,.35)}
dialog h3{margin:0 0 8px;font-size:17px}
dialog p{margin:0 0 14px;color:var(--muted);font-size:14px}
dialog input{width:100%;border:1px solid var(--line);border-radius:8px;padding:8px 10px;background:var(--surface-2);margin-bottom:14px}
dialog .row{display:flex;justify-content:flex-end;gap:8px}
.danger-fill{background:var(--danger)!important;border-color:var(--danger)!important;color:#fff!important}
</style>
</head>
<body>
<header>
  <div class="brand">
    <h1 id="boardTitle">Retro Board</h1>
    <button class="icon" id="renameBoard" title="Rename board" hidden></button>
    <span class="anon" title="No names, IP addresses or timestamps are recorded">Anonymous</span>
  </div>
  <div class="tools">
    <label class="toggle"><input type="checkbox" id="sortVotes"> Sort by votes</label>
    <span class="conn" id="conn" title="Connected"></span>
    <button class="btn" id="adminBtn">Facilitator</button>
  </div>
</header>

<div class="adminbar" id="adminbar">
  <span class="label">Facilitator</span>
  <button class="btn primary" id="revealBtn">Reveal cards</button>
  <button class="btn" id="addColBtn">+ Add column</button>
  <span class="sep"></span>
  <span>Export:</span>
  <button class="btn" data-export="md">Markdown</button>
  <button class="btn" data-export="csv">CSV</button>
  <button class="btn" data-export="json">JSON</button>
  <span class="sep"></span>
  <button class="btn danger" id="clearBtn">Clear board</button>
  <button class="btn" id="logoutBtn" style="margin-left:auto">Exit facilitator mode</button>
</div>

<div class="banner" id="banner"></div>
<main id="board"></main>
<div class="toast" id="toast"></div>

<dialog id="dlg">
  <form method="dialog" id="dlgForm">
    <h3 id="dlgTitle"></h3>
    <p id="dlgMsg"></p>
    <input id="dlgInput" autocomplete="off">
    <div class="row">
      <button class="btn" value="cancel" type="button" id="dlgCancel">Cancel</button>
      <button class="btn primary" value="ok" id="dlgOk">OK</button>
    </div>
  </form>
</dialog>

<script>
(function(){
"use strict";
const COLOR_KEYS = ["well","notwell","actions","purple","amber","teal","pink","slate"];
function colColors(col){
  const k = col.color || col.id;
  return COLOR_KEYS.includes(k) ? ["var(--" + k + ")", "var(--" + k + "-bg)"] : ["var(--accent)","var(--surface-2)"];
}
const ICON = {
  pencil:'<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M12 20h9"/><path d="M16.5 3.5a2.1 2.1 0 0 1 3 3L7 19l-4 1 1-4Z"/></svg>',
  trash:'<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M3 6h18"/><path d="M8 6V4h8v2"/><path d="M19 6l-1 14H6L5 6"/></svg>',
  up:'<svg viewBox="0 0 24 24" fill="currentColor"><path d="M12 4l8 10h-5v6H9v-6H4z"/></svg>'
};

// ---- storage (per-browser, never sent anywhere except card keys/voter token)
const store = {
  get(k, d){ try{ const v = localStorage.getItem(k); return v==null ? d : JSON.parse(v); }catch(e){ return d; } },
  set(k, v){ try{ localStorage.setItem(k, JSON.stringify(v)); }catch(e){} }
};
function randHex(n){ const a = new Uint8Array(n); crypto.getRandomValues(a); return Array.from(a, b => b.toString(16).padStart(2,"0")).join(""); }
let voter = store.get("retro.voter", null);
if(!voter){ voter = randHex(16); store.set("retro.voter", voter); }
let mine = store.get("retro.mine", {boardId:null, cards:{}});      // id -> {key, text}
let voted = store.get("retro.voted", {boardId:null, ids:[]});
let passcode = "";
try{ passcode = sessionStorage.getItem("retro.passcode") || ""; }catch(e){}

let state = null, version = 0, editingId = null;
const drafts = {};
const $ = id => document.getElementById(id);
const sortBox = $("sortVotes");
sortBox.checked = !!store.get("retro.sort", false);
sortBox.onchange = () => { store.set("retro.sort", sortBox.checked); render(); };

// ---- helpers
function el(tag, attrs, ...kids){
  const e = document.createElement(tag);
  for(const k in (attrs||{})){
    const v = attrs[k];
    if(k === "class") e.className = v;
    else if(k === "html") e.innerHTML = v;   // only used with constant ICON strings
    else if(k.startsWith("on")) e[k] = v;
    else if(v === true) e.setAttribute(k, "");
    else if(v !== false && v != null) e.setAttribute(k, v);
  }
  for(const kid of kids){ if(kid != null) e.append(kid); }
  return e;
}
let toastTimer;
function toast(msg){ const t = $("toast"); t.textContent = msg; t.classList.add("on"); clearTimeout(toastTimer); toastTimer = setTimeout(() => t.classList.remove("on"), 2600); }

async function api(path, body){
  const headers = {"Content-Type":"application/json"};
  if(passcode) headers["X-Facilitator"] = passcode;
  const r = await fetch(path, {method:"POST", headers, body:JSON.stringify(body||{})});
  const j = await r.json().catch(() => ({}));
  if(!r.ok) throw new Error(j.error || ("Request failed (" + r.status + ")"));
  return j;
}
async function run(fn){ try{ return await fn(); }catch(e){ toast(e.message); throw e; } }

function ask({title, message="", input=null, ok="OK", danger=false}){
  return new Promise(resolve => {
    const d = $("dlg"), inp = $("dlgInput"), okBtn = $("dlgOk");
    $("dlgTitle").textContent = title;
    $("dlgMsg").textContent = message;
    $("dlgMsg").style.display = message ? "" : "none";
    inp.style.display = input ? "" : "none";
    if(input){ inp.type = input.type || "text"; inp.value = input.value || ""; inp.placeholder = input.placeholder || ""; }
    okBtn.textContent = ok;
    okBtn.classList.toggle("danger-fill", danger);
    const done = val => { d.close(); resolve(val); };
    $("dlgCancel").onclick = () => done(null);
    $("dlgForm").onsubmit = ev => { ev.preventDefault(); done(input ? inp.value : true); };
    d.oncancel = ev => { ev.preventDefault(); done(null); };
    d.showModal();
    if(input){ inp.focus(); inp.select(); } else okBtn.focus();
  });
}

// ---- live updates (long polling)
async function poll(){
  for(;;){
    try{
      const r = await fetch("/api/state?v=" + version, {cache:"no-store"});
      if(!r.ok) throw new Error();
      const s = await r.json();
      $("conn").classList.remove("off"); $("conn").title = "Connected";
      if(s.version !== version){ version = s.version; apply(s); }
    }catch(e){
      $("conn").classList.add("off"); $("conn").title = "Disconnected — retrying…";
      await new Promise(res => setTimeout(res, 2000));
    }
  }
}

function apply(s){
  if(mine.boardId !== s.boardId){ mine = {boardId:s.boardId, cards:{}}; store.set("retro.mine", mine); }
  if(voted.boardId !== s.boardId){ voted = {boardId:s.boardId, ids:[]}; store.set("retro.voted", voted); }
  // Keep cached text of own cards current once revealed.
  if(!s.hidden){ for(const c of s.cards){ if(mine.cards[c.id]) mine.cards[c.id].text = c.text; } store.set("retro.mine", mine); }
  state = s;
  render();
}

// ---- rendering
function render(){
  if(!state) return;
  document.title = state.title + " · Retro Board";
  $("boardTitle").textContent = state.title;
  const admin = !!passcode;
  $("adminbar").classList.toggle("on", admin);
  $("adminBtn").hidden = admin;
  const rb = $("renameBoard"); rb.hidden = !admin; rb.innerHTML = ICON.pencil;
  $("revealBtn").textContent = state.hidden ? "Reveal cards" : "Hide cards";

  const banner = $("banner");
  const total = state.cards.length;
  banner.classList.toggle("on", state.hidden);
  banner.textContent = state.hidden
    ? "Cards are hidden while everyone writes. You can see your own cards; the facilitator will reveal the rest. " + total + " card" + (total === 1 ? "" : "s") + " posted so far. Voting opens on reveal."
    : "";

  const board = $("board");
  const existing = {};
  board.querySelectorAll(".col").forEach(c => existing[c.dataset.id] = c);
  const order = [];
  for(const col of state.columns){
    let node = existing[col.id];
    if(!node){ node = buildColumn(col); }
    updateColumn(node, col, admin);
    order.push(node);
  }
  board.replaceChildren(...order);
}

function buildColumn(col){
  const [c, cbg] = colColors(col);
  const ta = el("textarea", {placeholder:"Add a card…", maxlength:"1000", "aria-label":"New card for " + col.title});
  ta.value = drafts[col.id] || "";
  ta.oninput = () => { drafts[col.id] = ta.value; };
  const addBtn = el("button", {class:"btn", type:"submit"}, "Add");
  const form = el("form", {class:"add"}, ta, el("div", {class:"add-row"}, el("span", {}, "Enter to add · Shift+Enter for new line"), addBtn));
  ta.onkeydown = ev => { if(ev.key === "Enter" && !ev.shiftKey && !ev.isComposing){ ev.preventDefault(); form.requestSubmit(); } };
  form.onsubmit = async ev => {
    ev.preventDefault();
    const text = ta.value.trim(); if(!text) return;
    addBtn.disabled = true;
    try{
      const r = await run(() => api("/api/cards/add", {column:col.id, text}));
      if(mine.boardId !== r.boardId) mine = {boardId:r.boardId, cards:{}};
      mine.cards[r.id] = {key:r.key, text};
      store.set("retro.mine", mine);
      ta.value = ""; drafts[col.id] = "";
      render();
    }catch(e){} finally{ addBtn.disabled = false; ta.focus(); }
  };
  const node = el("section", {class:"col", "data-id":col.id},
    el("div", {class:"col-head"}, el("div", {class:"col-title"}), form),
    el("ul", {class:"cards"}));
  node.style.setProperty("--c", c); node.style.setProperty("--cbg", cbg);
  return node;
}

function updateColumn(node, col, admin){
  const cards = state.cards.filter(x => x.column === col.id);
  const head = node.querySelector(".col-title");
  if(!head.querySelector(".rename")){
    const kids = [el("h2", {}, col.title), el("span", {class:"count"}, String(cards.length))];
    if(admin){
      kids.push(el("button", {class:"icon", title:"Rename column", html:ICON.pencil, onclick:() => startRename(head, col)}));
      if(state.columns.length > 1) kids.push(el("button", {class:"icon", title:"Remove column", html:ICON.trash, onclick:() => removeColumn(col, cards.length)}));
    }
    head.replaceChildren(...kids);
  }
  node.querySelector("textarea").setAttribute("aria-label", "New card for " + col.title);

  let list = cards.slice();
  if(sortBox.checked && !state.hidden) list.sort((a, b) => b.votes - a.votes);
  const ul = node.querySelector(".cards");
  const keep = {};
  ul.querySelectorAll(".card.editing").forEach(li => keep[li.dataset.id] = li);
  const items = list.map(card => (card.id === editingId && keep[card.id]) ? keep[card.id] : buildCard(card, admin));
  if(!items.length) items.push(el("li", {class:"empty"}, state.hidden ? "No cards yet — or they're hidden" : "No cards yet"));
  ul.replaceChildren(...items);
}

function startRename(head, col){
  const inp = el("input", {value:col.title, maxlength:"80", "aria-label":"Column name"});
  const save = async () => {
    const t = inp.value.trim();
    head.replaceChildren();
    if(t && t !== col.title){ try{ await run(() => api("/api/admin/rename-column", {column:col.id, title:t})); }catch(e){} }
    render();
  };
  const wrap = el("div", {class:"rename"}, inp,
    el("button", {class:"btn", type:"button", onclick:save}, "Save"));
  inp.onkeydown = ev => { if(ev.key === "Enter"){ ev.preventDefault(); save(); } if(ev.key === "Escape"){ head.replaceChildren(); render(); } };
  head.replaceChildren(wrap);
  inp.focus(); inp.select();
}

function buildCard(card, admin){
  const own = mine.cards[card.id];
  const masked = state.hidden && !own;
  let text = card.text;
  if(state.hidden) text = own ? (own.text || "(your card)") : "Hidden until the facilitator reveals cards";
  const li = el("li", {class:"card" + (own ? " mine" : "") + (masked ? " masked" : ""), "data-id":card.id});
  const p = el("p", {class:"text"}, text);
  const isVoted = voted.ids.includes(card.id);
  const voteBtn = el("button", {class:"vote" + (isVoted ? " on" : ""), disabled:state.hidden,
    title: state.hidden ? "Voting opens when cards are revealed" : (isVoted ? "Remove your vote" : "Vote for this card"),
    "aria-pressed": isVoted ? "true" : "false", html:ICON.up});
  voteBtn.append(String(card.votes));
  voteBtn.onclick = async () => {
    voteBtn.disabled = true;
    try{
      const r = await run(() => api("/api/vote", {id:card.id, voter}));
      voted.ids = voted.ids.filter(x => x !== card.id);
      if(r.voted) voted.ids.push(card.id);
      store.set("retro.voted", voted);
    }catch(e){ voteBtn.disabled = false; }
  };
  const meta = el("div", {class:"meta"}, own ? el("span", {class:"tag"}, state.hidden ? "Your card · only you can see it" : "Your card") : el("span", {class:"spacer"}));
  if(own) meta.append(el("button", {class:"icon", title:"Edit", html:ICON.pencil, onclick:() => startEdit(li, card)}));
  if(own || admin) meta.append(el("button", {class:"icon", title: own ? "Delete" : "Delete (facilitator)", html:ICON.trash, onclick:() => removeCard(card, own)}));
  meta.append(voteBtn);
  li.append(p, meta);
  return li;
}

function startEdit(li, card){
  const own = mine.cards[card.id]; if(!own) return;
  editingId = card.id;
  li.classList.add("editing");
  const ta = el("textarea", {class:"edit-area", maxlength:"1000", "aria-label":"Edit card"});
  ta.value = card.text != null ? card.text : (own.text || "");
  const finish = () => { editingId = null; render(); };
  const save = async () => {
    const t = ta.value.trim();
    if(!t) return toast("Card can't be empty.");
    try{
      await run(() => api("/api/cards/edit", {id:card.id, key:own.key, text:t}));
      own.text = t; store.set("retro.mine", mine);
    }catch(e){}
    finish();
  };
  ta.onkeydown = ev => {
    if(ev.key === "Enter" && !ev.shiftKey && !ev.isComposing){ ev.preventDefault(); save(); }
    if(ev.key === "Escape") finish();
  };
  li.replaceChildren(ta, el("div", {class:"edit-actions"},
    el("button", {class:"btn", type:"button", onclick:finish}, "Cancel"),
    el("button", {class:"btn primary", type:"button", onclick:save}, "Save")));
  ta.focus(); ta.setSelectionRange(ta.value.length, ta.value.length);
}

async function removeColumn(col, count){
  const msg = count ? "This also deletes its " + count + " card" + (count === 1 ? "" : "s") + " for everyone. Export first if you need them." : "The column is empty.";
  const okd = await ask({title:"Remove \u201c" + col.title + "\u201d?", message:msg, ok:"Remove column", danger:true});
  if(!okd) return;
  try{ await run(() => api("/api/admin/delete-column", {column:col.id})); delete drafts[col.id]; }catch(e){}
}

async function removeCard(card, own){
  const okd = await ask({title:"Delete this card?", message: own ? "This removes your card for everyone." : "As facilitator you're deleting someone else's card.", ok:"Delete", danger:true});
  if(!okd) return;
  try{
    await run(() => api("/api/cards/delete", {id:card.id, key: own ? own.key : null}));
    delete mine.cards[card.id]; store.set("retro.mine", mine);
  }catch(e){}
}

// ---- facilitator
$("adminBtn").onclick = async () => {
  const code = await ask({title:"Facilitator mode", message:"Enter the passcode shown in the terminal where the board was started.", input:{type:"password", placeholder:"Passcode"}, ok:"Unlock"});
  if(!code) return;
  const prev = passcode; passcode = code.trim();
  try{
    await api("/api/admin/check");
    try{ sessionStorage.setItem("retro.passcode", passcode); }catch(e){}
    toast("Facilitator mode on");
  }catch(e){ passcode = prev; toast("Wrong passcode"); }
  render();
};
$("logoutBtn").onclick = () => { passcode = ""; try{ sessionStorage.removeItem("retro.passcode"); }catch(e){} render(); };
$("revealBtn").onclick = () => run(() => api("/api/admin/hidden", {hidden: !state.hidden})).catch(() => {});
$("addColBtn").onclick = async () => {
  const t = await ask({title:"Add column", input:{placeholder:"Column name, e.g. Shout-outs"}, ok:"Add"});
  if(t && t.trim()) run(() => api("/api/admin/add-column", {title:t.trim()})).catch(() => {});
};
$("renameBoard").onclick = async () => {
  const t = await ask({title:"Rename board", input:{value:state.title}, ok:"Save"});
  if(t && t.trim()) run(() => api("/api/admin/rename-board", {title:t.trim()})).catch(() => {});
};
$("clearBtn").onclick = async () => {
  const okd = await ask({title:"Clear the board?", message:"All cards and votes will be removed and cards will be hidden again for the next retro. A copy is archived as a JSON file next to the board data. Export first if you need a Markdown or CSV copy.", ok:"Clear board", danger:true});
  if(!okd) return;
  try{ const r = await run(() => api("/api/admin/clear")); toast(r.archived ? "Board cleared · archived to " + r.archived : "Board cleared"); }catch(e){}
};
document.querySelectorAll("[data-export]").forEach(b => b.onclick = async () => {
  const fmt = b.dataset.export;
  if(state && state.hidden){
    const okd = await ask({title:"Cards are still hidden", message:"The export will include every card, hidden ones too. Continue?", ok:"Export"});
    if(!okd) return;
  }
  try{
    const r = await fetch("/api/export?format=" + fmt, {headers:{"X-Facilitator":passcode}, cache:"no-store"});
    if(!r.ok){ const j = await r.json().catch(() => ({})); throw new Error(j.error || "Export failed"); }
    const blob = await r.blob();
    const m = /filename="([^"]+)"/.exec(r.headers.get("Content-Disposition") || "");
    const a = el("a", {href:URL.createObjectURL(blob), download: m ? m[1] : "retro." + fmt});
    document.body.append(a); a.click(); a.remove();
    setTimeout(() => URL.revokeObjectURL(a.href), 5000);
  }catch(e){ toast(e.message); }
});

poll();
})();
</script>
</body>
</html>
"""

if __name__ == "__main__":
    main()
