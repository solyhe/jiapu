#!/usr/bin/env python3
"""家谱树 - 轻量家谱平台后端 (仅依赖 Python 标准库 + sqlite3)"""
import base64, gzip, hmac, json, os, re, sqlite3, threading, time
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler

DB_PATH = os.environ.get("DB_PATH", "/data/family.db")
PORT = int(os.environ.get("PORT", "8080"))
AUTH_USER = os.environ.get("AUTH_USER", "admin")
AUTH_PASS = os.environ.get("AUTH_PASS", "")          # 设置后启用 Basic 认证
READ_PUBLIC = os.environ.get("READ_PUBLIC", "0") == "1"  # 1=游客可只读浏览
MAX_BACKUPS = int(os.environ.get("MAX_BACKUPS", "50"))
STATIC = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")
MAX_BODY = 20 * 1024 * 1024
FIELDS = ["id", "name", "gender", "alive", "birth", "death", "zibei", "origin",
          "job", "bio", "parent_id", "spouse_of", "ord"]
LIMITS = {"id": 32, "name": 40, "birth": 16, "death": 16, "zibei": 40,
          "origin": 80, "job": 80, "bio": 4000, "parent_id": 32, "spouse_of": 32}

_local = threading.local()
_wlock = threading.Lock()


def db():
    c = getattr(_local, "c", None)
    if c is None:
        c = sqlite3.connect(DB_PATH, timeout=10)
        c.row_factory = sqlite3.Row
        c.execute("PRAGMA foreign_keys=ON")
        c.execute("PRAGMA busy_timeout=5000")
        _local.c = c
    return c


def init_db():
    os.makedirs(os.path.dirname(DB_PATH) or ".", exist_ok=True)
    c = sqlite3.connect(DB_PATH)
    c.execute("PRAGMA journal_mode=WAL")
    c.executescript("""
    CREATE TABLE IF NOT EXISTS trees(
      id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL,
      version INTEGER NOT NULL DEFAULT 0, updated_at TEXT);
    CREATE TABLE IF NOT EXISTS members(
      tree_id INTEGER NOT NULL REFERENCES trees(id) ON DELETE CASCADE,
      id TEXT NOT NULL, name TEXT NOT NULL DEFAULT '', gender TEXT DEFAULT 'u',
      alive INTEGER DEFAULT 1, birth TEXT, death TEXT, zibei TEXT, origin TEXT,
      job TEXT, bio TEXT, parent_id TEXT, spouse_of TEXT, ord INTEGER DEFAULT 0,
      PRIMARY KEY(tree_id, id));
    CREATE INDEX IF NOT EXISTS ix_m_name ON members(tree_id, name);
    CREATE TABLE IF NOT EXISTS backups(
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      tree_id INTEGER NOT NULL REFERENCES trees(id) ON DELETE CASCADE,
      created_at TEXT, note TEXT, count INTEGER, data TEXT NOT NULL);
    """)
    if c.execute("SELECT COUNT(*) FROM trees").fetchone()[0] == 0:
        c.execute("INSERT INTO trees(name,version,updated_at) VALUES(?,0,?)", ("我的家谱", now()))
    c.commit()
    c.close()


def now():
    return time.strftime("%Y-%m-%d %H:%M:%S")


def clean_members(raw):
    if not isinstance(raw, list) or len(raw) > 50000:
        raise ValueError("members 必须是数组且不超过 50000 条")
    out, ids = [], set()
    for m in raw:
        if not isinstance(m, dict):
            continue
        r = {}
        for f in FIELDS:
            v = m.get(f)
            if f in ("alive", "ord"):
                try:
                    v = int(v or 0) if f == "ord" else (0 if v in (0, "0", False) else 1)
                except (TypeError, ValueError):
                    v = 0
            elif f == "gender":
                v = v if v in ("m", "f", "u") else "u"
            else:
                v = "" if v is None else str(v).strip()[:LIMITS.get(f, 200)]
            r[f] = v
        if not r["id"] or not re.fullmatch(r"[A-Za-z0-9_-]+", r["id"]) or r["id"] in ids:
            continue
        ids.add(r["id"])
        out.append(r)
    for r in out:  # 清理悬空引用
        if r["parent_id"] not in ids or r["parent_id"] == r["id"]:
            r["parent_id"] = ""
        if r["spouse_of"] not in ids or r["spouse_of"] == r["id"]:
            r["spouse_of"] = ""
    return out


def load_members(tid):
    rows = db().execute("SELECT * FROM members WHERE tree_id=? ORDER BY ord, rowid", (tid,)).fetchall()
    return [{f: r[f] for f in FIELDS} for r in rows]


def write_members(c, tid, members):
    c.execute("DELETE FROM members WHERE tree_id=?", (tid,))
    c.executemany(
        "INSERT INTO members(tree_id," + ",".join(FIELDS) + ") VALUES(?," + ",".join("?" * len(FIELDS)) + ")",
        [(tid, *[m[f] for f in FIELDS]) for m in members])


def make_backup(c, tid, note):
    data = json.dumps(load_members(tid), ensure_ascii=False)
    cnt = c.execute("SELECT COUNT(*) FROM members WHERE tree_id=?", (tid,)).fetchone()[0]
    c.execute("INSERT INTO backups(tree_id,created_at,note,count,data) VALUES(?,?,?,?,?)",
              (tid, now(), note[:100], cnt, data))
    c.execute("""DELETE FROM backups WHERE tree_id=? AND id NOT IN
                 (SELECT id FROM backups WHERE tree_id=? ORDER BY id DESC LIMIT ?)""",
              (tid, tid, MAX_BACKUPS))


class Err(Exception):
    def __init__(self, code, msg):
        self.code, self.msg = code, msg


class H(BaseHTTPRequestHandler):
    server_version = "Jiapu/1.0"
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *a):
        if os.environ.get("QUIET") != "1":
            super().log_message(fmt, *a)

    # ---------- helpers ----------
    def send(self, code, body=b"", ctype="application/json; charset=utf-8", extra=None):
        if isinstance(body, (dict, list)):
            body = json.dumps(body, ensure_ascii=False).encode()
        elif isinstance(body, str):
            body = body.encode()
        if len(body) > 1024 and "gzip" in self.headers.get("Accept-Encoding", ""):
            body = gzip.compress(body, 6)
            extra = dict(extra or {}, **{"Content-Encoding": "gzip"})
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("X-Content-Type-Options", "nosniff")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def authed(self):
        if not AUTH_PASS:
            return True
        h = self.headers.get("Authorization", "")
        if not h.startswith("Basic "):
            return False
        try:
            u, _, p = base64.b64decode(h[6:]).decode().partition(":")
        except Exception:
            return False
        return hmac.compare_digest(u, AUTH_USER) and hmac.compare_digest(p, AUTH_PASS)

    def body(self):
        n = int(self.headers.get("Content-Length") or 0)
        if n > MAX_BODY:
            raise Err(413, "请求体过大")
        try:
            return json.loads(self.rfile.read(n) or b"{}")
        except ValueError:
            raise Err(400, "JSON 格式错误")

    def tree(self, tid):
        r = db().execute("SELECT * FROM trees WHERE id=?", (tid,)).fetchone()
        if not r:
            raise Err(404, "家谱不存在")
        return r

    # ---------- dispatch ----------
    def handle_any(self, method):
        path = self.path.split("?", 1)[0]
        write = method not in ("GET", "HEAD")
        if not self.authed() and (write or not READ_PUBLIC or path == "/api/login"):
            return self.send(401, {"error": "需要登录"},
                             extra={"WWW-Authenticate": 'Basic realm="jiapu", charset="UTF-8"'})
        try:
            if not path.startswith("/api/"):
                if method not in ("GET", "HEAD"):
                    raise Err(405, "不支持")
                return self.static(path)
            if write:
                with _wlock:
                    return self.api(method, path)
            return self.api(method, path)
        except Err as e:
            self.send(e.code, {"error": e.msg})
        except ValueError as e:
            self.send(400, {"error": str(e)})
        except Exception as e:  # noqa
            self.send(500, {"error": "服务器错误: %s" % e})

    def static(self, path):
        if path in ("/", "/index.html"):
            fn, ct = "index.html", "text/html; charset=utf-8"
        elif path == "/favicon.svg":
            fn, ct = "favicon.svg", "image/svg+xml"
        else:
            raise Err(404, "Not found")
        with open(os.path.join(STATIC, fn), "rb") as f:
            self.send(200, f.read(), ct, {"Cache-Control": "no-cache"})

    def api(self, method, path):
        c = db()
        p = path.strip("/").split("/")[1:]  # 去掉 "api"
        if p == ["health"]:
            return self.send(200, {"ok": True})
        if p == ["me"]:
            return self.send(200, {"canEdit": self.authed(), "auth": bool(AUTH_PASS)})
        if p == ["login"]:
            return self.send(200, {"ok": True})
        if p == ["trees"]:
            if method == "GET":
                rows = c.execute("""SELECT t.id,t.name,t.updated_at,
                    (SELECT COUNT(*) FROM members m WHERE m.tree_id=t.id) AS count
                    FROM trees t ORDER BY t.id""").fetchall()
                return self.send(200, [dict(r) for r in rows])
            if method == "POST":
                name = str(self.body().get("name") or "新家谱").strip()[:40]
                cur = c.execute("INSERT INTO trees(name,version,updated_at) VALUES(?,0,?)", (name, now()))
                c.commit()
                return self.send(201, {"id": cur.lastrowid, "name": name})
        if len(p) >= 2 and p[0] == "trees" and p[1].isdigit():
            tid = int(p[1])
            t = self.tree(tid)
            if len(p) == 2:
                if method == "GET":
                    return self.send(200, {"id": tid, "name": t["name"], "version": t["version"],
                                           "updated_at": t["updated_at"], "members": load_members(tid)})
                if method == "PUT":
                    b = self.body()
                    if b.get("version") is not None and int(b["version"]) != t["version"]:
                        raise Err(409, "家谱已在别处被修改，请刷新后再试")
                    members = clean_members(b.get("members", []))
                    ver = t["version"] + 1
                    with c:
                        if b.get("backup"):
                            make_backup(c, tid, str(b["backup"]))
                        write_members(c, tid, members)
                        c.execute("UPDATE trees SET version=?,updated_at=? WHERE id=?", (ver, now(), tid))
                    return self.send(200, {"version": ver, "updated_at": now(), "count": len(members)})
                if method == "PATCH":
                    name = str(self.body().get("name") or "").strip()[:40]
                    if not name:
                        raise Err(400, "名称不能为空")
                    with c:
                        c.execute("UPDATE trees SET name=? WHERE id=?", (name, tid))
                    return self.send(200, {"ok": True})
                if method == "DELETE":
                    if c.execute("SELECT COUNT(*) FROM trees").fetchone()[0] <= 1:
                        raise Err(400, "至少保留一个家谱")
                    with c:
                        c.execute("DELETE FROM trees WHERE id=?", (tid,))
                    return self.send(200, {"ok": True})
            if p[2:] == ["backups"]:
                if method == "GET":
                    rows = c.execute("SELECT id,created_at,note,count FROM backups WHERE tree_id=? ORDER BY id DESC",
                                     (tid,)).fetchall()
                    return self.send(200, [dict(r) for r in rows])
                if method == "POST":
                    with c:
                        make_backup(c, tid, str(self.body().get("note") or "手动备份"))
                    return self.send(201, {"ok": True})
        if len(p) >= 2 and p[0] == "backups" and p[1].isdigit():
            bid = int(p[1])
            b = c.execute("SELECT * FROM backups WHERE id=?", (bid,)).fetchone()
            if not b:
                raise Err(404, "备份不存在")
            if p[2:] == ["restore"] and method == "POST":
                tid = b["tree_id"]
                t = self.tree(tid)
                with c:
                    make_backup(c, tid, "恢复前自动备份")
                    write_members(c, tid, clean_members(json.loads(b["data"])))
                    c.execute("UPDATE trees SET version=?,updated_at=? WHERE id=?", (t["version"] + 1, now(), tid))
                return self.send(200, {"ok": True})
            if p[2:] == ["download"] and method == "GET":
                return self.send(200, b["data"], extra={
                    "Content-Disposition": 'attachment; filename="backup-%d.json"' % bid})
            if len(p) == 2 and method == "DELETE":
                with c:
                    c.execute("DELETE FROM backups WHERE id=?", (bid,))
                return self.send(200, {"ok": True})
        raise Err(404, "接口不存在")

    def do_GET(self): self.handle_any("GET")
    def do_HEAD(self): self.handle_any("HEAD")
    def do_POST(self): self.handle_any("POST")
    def do_PUT(self): self.handle_any("PUT")
    def do_PATCH(self): self.handle_any("PATCH")
    def do_DELETE(self): self.handle_any("DELETE")


if __name__ == "__main__":
    init_db()
    srv = ThreadingHTTPServer(("0.0.0.0", PORT), H)
    srv.daemon_threads = True
    print("家谱树已启动: http://0.0.0.0:%d  数据库: %s  认证: %s" % (PORT, DB_PATH, "开启" if AUTH_PASS else "关闭"), flush=True)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
