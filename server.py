"""
Nighthawks Corporation - license key + app field backend (Flask + SQLite)

Made to run on PythonAnywhere (free tier works). Only dependency: Flask.

Setup in short:
  1. Upload this folder to /home/<user>/nighthawks_backend
  2. In a Bash console:  cd ~/nighthawks_backend && python3 app.py init
     (creates the database and prints your ADMIN TOKEN once, keep it secret)
  3. Web tab -> Add a new web app -> Manual configuration (Flask) and point the
     WSGI file at this app (see README.md).

Settings (environment variables, all optional):
  NH_ADMIN_TOKEN       admin token (otherwise read from admin_token.txt)
  NH_ALLOWED_ORIGINS   website origin(s) allowed to call the admin API, comma
                       separated, e.g. https://nighthawks.example  (default: *)
  NH_BASE_URL          force the public base URL used when building field URLs,
                       e.g. https://yourname.pythonanywhere.com
  NH_DB_PATH           database file path (default: nighthawks.db next to app.py)

Times are UNIX timestamps in seconds (UTC).
"""
import base64
import collections
import html as htmlmod
import functools
import hmac
import json
import os
import re
import secrets
import sqlite3
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from urllib.parse import urljoin, urlparse

from flask import Flask, g, jsonify, redirect, request, url_for
from werkzeug.middleware.proxy_fix import ProxyFix

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.environ.get("NH_DB_PATH", os.path.join(BASE_DIR, "nighthawks.db"))
TOKEN_FILE = os.path.join(BASE_DIR, "admin_token.txt")
ALLOWED_ORIGINS = [o.strip() for o in os.environ.get("NH_ALLOWED_ORIGINS", "*").split(",") if o.strip()]
BASE_URL = os.environ.get("NH_BASE_URL", "").rstrip("/")

# Month = 30 days. "lifetime" never expires.
UNITS = {"minutes": 60, "hours": 3600, "days": 86400, "weeks": 604800, "months": 2592000, "lifetime": 0}
VERSION_RE = re.compile(r"^[A-Za-z0-9._-]{1,32}$")
FIELD_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
KEY_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"

app = Flask(__name__)
app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)  # PythonAnywhere sits behind a proxy
app.json.sort_keys = False
app.config["MAX_CONTENT_LENGTH"] = 64 * 1024


# --------------------------------------------------------------------------- database
SCHEMA = """
CREATE TABLE IF NOT EXISTS plans(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  name TEXT NOT NULL UNIQUE,
  value INTEGER NOT NULL,
  unit TEXT NOT NULL,
  created_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS apps(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  name TEXT NOT NULL UNIQUE,
  slug TEXT NOT NULL UNIQUE,
  current_version TEXT NOT NULL,
  created_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS app_versions(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  app_id INTEGER NOT NULL REFERENCES apps(id) ON DELETE CASCADE,
  version TEXT NOT NULL,
  note TEXT NOT NULL DEFAULT '',
  created_at INTEGER NOT NULL,
  UNIQUE(app_id, version)
);
CREATE TABLE IF NOT EXISTS fields(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  app_id INTEGER NOT NULL REFERENCES apps(id) ON DELETE CASCADE,
  version TEXT NOT NULL,
  name TEXT NOT NULL,
  value TEXT NOT NULL,
  public INTEGER NOT NULL DEFAULT 0,
  updated_at INTEGER NOT NULL,
  UNIQUE(app_id, version, name)
);
CREATE TABLE IF NOT EXISTS licenses(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  key TEXT NOT NULL UNIQUE,
  app_id INTEGER REFERENCES apps(id) ON DELETE SET NULL,
  app_name TEXT NOT NULL DEFAULT '',
  version TEXT NOT NULL DEFAULT '',
  plan_name TEXT NOT NULL,
  dur_value INTEGER NOT NULL,
  dur_unit TEXT NOT NULL,
  hwid_mode TEXT NOT NULL CHECK(hwid_mode IN ('hwid','open')),
  hwid TEXT NOT NULL DEFAULT '',
  status TEXT NOT NULL DEFAULT 'ok' CHECK(status IN ('ok','revoked')),
  created_at INTEGER NOT NULL,
  activated_at INTEGER NOT NULL DEFAULT 0,
  expires_at INTEGER NOT NULL DEFAULT 0,
  note TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS settings(
  key TEXT PRIMARY KEY,
  value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS discord_channels(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  name TEXT NOT NULL UNIQUE,
  webhook_url TEXT NOT NULL,
  created_at INTEGER NOT NULL
);
"""


def db():
    if "db" not in g:
        g.db = sqlite3.connect(DB_PATH, timeout=15)
        g.db.row_factory = sqlite3.Row
        g.db.execute("PRAGMA foreign_keys=ON")
    return g.db


@app.teardown_appcontext
def close_db(_exc):
    d = g.pop("db", None)
    if d is not None:
        d.close()


def init_db():
    con = sqlite3.connect(DB_PATH)
    con.executescript(SCHEMA)
    old = con.execute("SELECT value FROM settings WHERE key='discord_webhook'").fetchone()
    if old and not con.execute("SELECT 1 FROM discord_channels LIMIT 1").fetchone():
        cur = con.execute("INSERT INTO discord_channels(name,webhook_url,created_at) VALUES('Main',?,?)", (old[0], int(time.time())))
        for k in ("discord_route_new", "discord_route_price"):
            con.execute("INSERT OR REPLACE INTO settings(key,value) VALUES(?,?)", (k, json.dumps([cur.lastrowid])))
        con.execute("DELETE FROM settings WHERE key='discord_webhook'")
    con.commit()
    con.close()


init_db()


# --------------------------------------------------------------------------- helpers
def load_admin_token():
    t = os.environ.get("NH_ADMIN_TOKEN", "").strip()
    if t:
        return t
    try:
        with open(TOKEN_FILE, encoding="utf-8") as f:
            return f.read().strip()
    except OSError:
        return ""


def now():
    return int(time.time())


def err(status, code, message):
    return jsonify({"ok": False, "code": code, "message": message}), status


def body():
    data = request.get_json(silent=True)
    return data if isinstance(data, dict) else {}


def slugify(text):
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")[:48] or "app"


def duration_seconds(value, unit):
    return 0 if unit == "lifetime" else int(value) * UNITS[unit]


def clean_duration(value, unit):
    """Returns (value, unit) or raises ValueError."""
    if unit not in UNITS:
        raise ValueError("unit must be one of: " + ", ".join(UNITS))
    if unit == "lifetime":
        return 1, "lifetime"
    try:
        value = int(value)
    except (TypeError, ValueError):
        raise ValueError("duration value must be a whole number")
    if not 1 <= value <= 100000:
        raise ValueError("duration value must be between 1 and 100000")
    return value, unit


def field_url(app_slug, version, name):
    path = url_for("get_field", app_slug=app_slug, version=version, name=name)
    return BASE_URL + path if BASE_URL else url_for("get_field", app_slug=app_slug, version=version, name=name, _external=True)


def gen_key():
    raw = "".join(secrets.choice(KEY_ALPHABET) for _ in range(16))
    return "NH-" + "-".join(raw[i:i + 4] for i in range(0, 16, 4))


def license_state(row):
    if row["status"] == "revoked":
        return "revoked"
    if not row["activated_at"]:
        return "unused"
    if row["expires_at"] and now() > row["expires_at"]:
        return "expired"
    return "active"


def license_json(row):
    return {
        "id": row["id"],
        "key": row["key"],
        "app_id": row["app_id"],
        "app": row["app_name"],
        "version": row["version"],
        "plan": row["plan_name"],
        "duration": {"value": row["dur_value"], "unit": row["dur_unit"]},
        "hwid_mode": row["hwid_mode"],
        "hwid": row["hwid"],
        "status": license_state(row),
        "created_at": row["created_at"],
        "activated_at": row["activated_at"] or None,
        "expires_at": row["expires_at"] or None,
        "note": row["note"],
    }


def check_license(key, hwid, activate):
    """Core license check. Returns (row, None) on success or (None, (code, message))."""
    key = str(key or "").strip().upper()
    hwid = str(hwid or "").strip()[:256]
    row = db().execute("SELECT * FROM licenses WHERE key=?", (key,)).fetchone()
    if not row:
        return None, ("invalid_key", "Invalid license key.")
    if row["status"] == "revoked":
        return None, ("revoked", "This key has been revoked.")
    if row["hwid_mode"] == "hwid":
        if not hwid:
            return None, ("hwid_required", "This key is HWID locked. Send your HWID.")
        if row["hwid"] and not hmac.compare_digest(row["hwid"], hwid):
            return None, ("hwid_mismatch", "This key is bound to a different device.")
    t = now()
    if row["activated_at"] and row["expires_at"] and t > row["expires_at"]:
        return None, ("expired", "This key has expired.")
    if not row["activated_at"]:
        if not activate:
            return None, ("not_activated", "Key is not activated yet. Validate it first.")
        secs = duration_seconds(row["dur_value"], row["dur_unit"])
        db().execute(
            "UPDATE licenses SET activated_at=?, expires_at=? WHERE id=? AND activated_at=0",
            (t, t + secs if secs else 0, row["id"]),
        )
    if row["hwid_mode"] == "hwid" and not row["hwid"]:
        db().execute("UPDATE licenses SET hwid=? WHERE id=? AND hwid=''", (hwid, row["id"]))
    db().commit()
    row = db().execute("SELECT * FROM licenses WHERE id=?", (row["id"],)).fetchone()
    # if two devices raced to bind, only the winner passes
    if row["hwid_mode"] == "hwid" and not hmac.compare_digest(row["hwid"], hwid):
        return None, ("hwid_mismatch", "This key is bound to a different device.")
    return row, None


_hits = collections.defaultdict(collections.deque)


def rate_limit(limit=30, per=60):
    def deco(f):
        @functools.wraps(f)
        def wrapper(*a, **k):
            if len(_hits) > 5000:
                _hits.clear()
            q = _hits[(f.__name__, request.remote_addr or "?")]
            t = time.time()
            while q and q[0] < t - per:
                q.popleft()
            if len(q) >= limit:
                return err(429, "rate_limited", "Too many requests. Slow down.")
            q.append(t)
            return f(*a, **k)
        return wrapper
    return deco


def admin_only(f):
    @functools.wraps(f)
    def wrapper(*a, **k):
        token = load_admin_token()
        if not token:
            return err(503, "no_admin_token", "Admin token not set. Run: python3 app.py init")
        sent = request.headers.get("X-Admin-Token", "")
        auth = request.headers.get("Authorization", "")
        if not sent and auth.lower().startswith("bearer "):
            sent = auth[7:]
        if not hmac.compare_digest(sent.encode(), token.encode()):
            return err(401, "unauthorized", "Bad or missing admin token.")
        return f(*a, **k)
    return wrapper


@app.after_request
def cors(resp):
    origin = request.headers.get("Origin")
    if origin and ("*" in ALLOWED_ORIGINS or origin in ALLOWED_ORIGINS):
        resp.headers["Access-Control-Allow-Origin"] = "*" if "*" in ALLOWED_ORIGINS else origin
        resp.headers["Vary"] = "Origin"
        resp.headers["Access-Control-Allow-Headers"] = "Content-Type, Authorization, X-Admin-Token, X-License-Key, X-HWID"
        resp.headers["Access-Control-Allow-Methods"] = "GET, POST, PUT, DELETE, OPTIONS"
        resp.headers["Access-Control-Max-Age"] = "600"
    resp.headers["Cache-Control"] = "no-store"
    resp.headers["X-Content-Type-Options"] = "nosniff"
    return resp


@app.route("/api/<path:_p>", methods=["OPTIONS"])
def preflight(_p):
    return ("", 204)


@app.errorhandler(404)
def e404(_e):
    return err(404, "not_found", "Not found.")


@app.errorhandler(405)
def e405(_e):
    return err(405, "method_not_allowed", "Method not allowed.")


@app.errorhandler(413)
def e413(_e):
    return err(413, "too_large", "Request too large.")


@app.errorhandler(500)
def e500(_e):
    return err(500, "server_error", "Server error.")


# --------------------------------------------------------------------------- public API
@app.get("/")
def index():
    return jsonify({"service": "Nighthawks license backend", "ok": True})


@app.get("/api/v1/ping")
def ping():
    return jsonify({"ok": True, "server_time": now()})


@app.post("/api/v1/license/validate")
@rate_limit(limit=30, per=60)
def validate():
    """Body: {"key": "NH-....", "hwid": "..."}. Activates a fresh key (timer starts now)."""
    b = body()
    row, e = check_license(b.get("key"), b.get("hwid"), activate=True)
    if e:
        return err(401, e[0], e[1])
    return jsonify({
        "ok": True,
        "message": "Valid",
        "app": row["app_name"],
        "version": row["version"],
        "plan": row["plan_name"],
        "hwid_mode": row["hwid_mode"],
        "activated_at": row["activated_at"],
        "expires_at": row["expires_at"] or None,
        "lifetime": not row["expires_at"],
        "server_time": now(),
    })


# ---- MediaFire direct downloads: turns a MediaFire share link into the real file link
MF_HOST_RE = re.compile(r"^(?:[a-z0-9-]+\.)*mediafire\.com$", re.I)
_mf_cache = {}


def mf_ok(url):
    """Only https links on mediafire.com (or its subdomains) are ever fetched or redirected to."""
    try:
        u = urlparse(url)
        return u.scheme == "https" and bool(u.hostname) and bool(MF_HOST_RE.match(u.hostname))
    except ValueError:
        return False


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


def mf_fetch(url):
    """Download the MediaFire page, following redirects by hand so every hop stays on mediafire.com."""
    opener = urllib.request.build_opener(_NoRedirect)
    for _ in range(4):
        if not mf_ok(url):
            raise ValueError("redirect left mediafire.com")
        req = urllib.request.Request(url, headers={
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36",
            "Accept-Language": "en-US,en;q=0.9"})
        try:
            with opener.open(req, timeout=10) as r:
                return r.read(2_000_000).decode("utf-8", "replace")
        except urllib.error.HTTPError as e:
            loc = e.headers.get("Location")
            if e.code in (301, 302, 303, 307, 308) and loc:
                url = urljoin(url, loc)
                continue
            raise
    raise ValueError("too many redirects")


def mf_direct(url):
    """Returns the direct download URL found on the MediaFire page, or None."""
    page = mf_fetch(url)
    m = re.search(r'data-scrambled-url="([^"]+)"', page)  # newer pages hide the link in base64
    if m:
        try:
            cand = base64.b64decode(m.group(1)).decode()
        except Exception:
            cand = ""
        if mf_ok(cand):
            return cand
    for m in re.finditer(r'href="(https://download[^"]*?\.mediafire\.com/[^"]+)"', page):
        cand = htmlmod.unescape(m.group(1))
        if mf_ok(cand):
            return cand
    return None


@app.get("/api/v1/download/mediafire")
@rate_limit(limit=20, per=60)
def mediafire_download():
    """GET ?url=<MediaFire share link>. Redirects the browser straight to the file.
    If the direct link can't be found, it redirects to the normal MediaFire page instead.
    Add &format=json to get {"url": ...} back instead of a redirect."""
    src = request.args.get("url", "").strip()
    if not mf_ok(src):
        return err(400, "bad_request", "url must be an https mediafire.com link.")
    hit = _mf_cache.get(src)
    if hit and hit[1] > time.time():
        direct = hit[0]
    else:
        try:
            direct = mf_direct(src)
        except Exception:
            direct = None
        if direct:
            if len(_mf_cache) > 500:
                _mf_cache.clear()
            _mf_cache[src] = (direct, time.time() + 60)
    if request.args.get("format") == "json":
        if not direct:
            return err(502, "not_found", "Could not find the direct download link.")
        return jsonify({"ok": True, "url": direct})
    return redirect(direct or src, 302)


@app.get("/api/v1/plans")
@rate_limit(limit=60, per=60)
def public_plans():
    """Subscription plans customers can see (names and lengths only)."""
    rows = db().execute("SELECT name,value,unit FROM plans ORDER BY id").fetchall()
    return jsonify({"ok": True, "plans": [dict(r) for r in rows]})


@app.post("/api/v1/license/info")
@rate_limit(limit=30, per=60)
def license_info():
    """Read-only look at a key the caller already holds. Does NOT activate or bind a device.
    Body: {"key": "NH-...."}"""
    key = str(body().get("key") or "").strip().upper()
    row = db().execute("SELECT * FROM licenses WHERE key=?", (key,)).fetchone()
    if not row:
        return err(404, "invalid_key", "That key was not found.")
    return jsonify({
        "ok": True,
        "app": row["app_name"],
        "version": row["version"],
        "plan": row["plan_name"],
        "duration": {"value": row["dur_value"], "unit": row["dur_unit"]},
        "hwid_mode": row["hwid_mode"],
        "hwid_bound": bool(row["hwid"]),
        "status": license_state(row),
        "activated_at": row["activated_at"] or None,
        "expires_at": row["expires_at"] or None,
        "lifetime": row["dur_unit"] == "lifetime",
        "server_time": now(),
    })


@app.get("/api/v1/fields/<app_slug>/<version>/<name>")
@rate_limit(limit=60, per=60)
def get_field(app_slug, version, name):
    """
    Request URL for one field. Use version 'latest' to follow the newest release.
    Headers for protected fields: X-License-Key and X-HWID (key must be activated first).
    Fields marked public need no key.
    """
    a = db().execute("SELECT * FROM apps WHERE slug=?", (app_slug,)).fetchone()
    if not a:
        return err(404, "not_found", "Unknown app.")
    ver = a["current_version"] if version == "latest" else version
    f = db().execute(
        "SELECT * FROM fields WHERE app_id=? AND version=? AND name=?", (a["id"], ver, name)
    ).fetchone()
    if not f:
        return err(404, "not_found", "Unknown field or version.")
    if not f["public"]:
        row, e = check_license(request.headers.get("X-License-Key"), request.headers.get("X-HWID"), activate=False)
        if e:
            return err(401, e[0], e[1])
        if row["app_id"] != a["id"]:
            return err(403, "wrong_app", "This key is not for this app.")
    return jsonify({"ok": True, "app": a["name"], "version": ver, "field": f["name"],
                    "value": f["value"], "updated_at": f["updated_at"]})


# --------------------------------------------------------------------------- admin API
@app.get("/api/v1/admin/check")
@admin_only
def admin_check():
    return jsonify({"ok": True})


# ---- subscription plans
@app.get("/api/v1/admin/plans")
@admin_only
def plans_list():
    rows = db().execute("SELECT * FROM plans ORDER BY id").fetchall()
    return jsonify({"ok": True, "plans": [dict(r) for r in rows]})


@app.post("/api/v1/admin/plans")
@admin_only
def plans_create():
    b = body()
    name = str(b.get("name", "")).strip()[:40]
    if not name:
        return err(400, "bad_request", "Plan name is required.")
    try:
        v, u = clean_duration(b.get("value", 1), b.get("unit"))
    except ValueError as ex:
        return err(400, "bad_request", str(ex))
    try:
        cur = db().execute("INSERT INTO plans(name,value,unit,created_at) VALUES(?,?,?,?)", (name, v, u, now()))
        db().commit()
    except sqlite3.IntegrityError:
        return err(409, "exists", "A plan with that name already exists.")
    return jsonify({"ok": True, "plan": {"id": cur.lastrowid, "name": name, "value": v, "unit": u}}), 201


@app.delete("/api/v1/admin/plans/<int:pid>")
@admin_only
def plans_delete(pid):
    db().execute("DELETE FROM plans WHERE id=?", (pid,))
    db().commit()
    return jsonify({"ok": True})


# ---- apps + versions
def app_json(a):
    vers = db().execute("SELECT version,note,created_at FROM app_versions WHERE app_id=? ORDER BY id", (a["id"],)).fetchall()
    return {"id": a["id"], "name": a["name"], "slug": a["slug"], "current_version": a["current_version"],
            "versions": [dict(v) for v in vers], "created_at": a["created_at"]}


@app.get("/api/v1/admin/apps")
@admin_only
def apps_list():
    rows = db().execute("SELECT * FROM apps ORDER BY name").fetchall()
    return jsonify({"ok": True, "apps": [app_json(a) for a in rows]})


@app.post("/api/v1/admin/apps")
@admin_only
def apps_create():
    b = body()
    name = str(b.get("name", "")).strip()[:60]
    ver = str(b.get("version", "1.0.0")).strip()
    if not name:
        return err(400, "bad_request", "App name is required.")
    if not VERSION_RE.match(ver):
        return err(400, "bad_request", "Version: letters, numbers, . _ - only (max 32).")
    slug, n = slugify(name), 2
    while db().execute("SELECT 1 FROM apps WHERE slug=?", (slug,)).fetchone():
        slug = f"{slugify(name)}-{n}"
        n += 1
    try:
        cur = db().execute("INSERT INTO apps(name,slug,current_version,created_at) VALUES(?,?,?,?)", (name, slug, ver, now()))
        db().execute("INSERT INTO app_versions(app_id,version,note,created_at) VALUES(?,?,?,?)",
                     (cur.lastrowid, ver, "Initial release", now()))
        db().commit()
    except sqlite3.IntegrityError:
        db().rollback()
        return err(409, "exists", "An app with that name already exists.")
    a = db().execute("SELECT * FROM apps WHERE id=?", (cur.lastrowid,)).fetchone()
    return jsonify({"ok": True, "app": app_json(a)}), 201


@app.delete("/api/v1/admin/apps/<int:aid>")
@admin_only
def apps_delete(aid):
    db().execute("DELETE FROM apps WHERE id=?", (aid,))  # versions + fields cascade, keys keep their app name
    db().commit()
    return jsonify({"ok": True})


@app.post("/api/v1/admin/apps/<int:aid>/versions")
@admin_only
def versions_release(aid):
    """Release a new version. All fields of the current version are copied into it."""
    a = db().execute("SELECT * FROM apps WHERE id=?", (aid,)).fetchone()
    if not a:
        return err(404, "not_found", "Unknown app.")
    b = body()
    ver = str(b.get("version", "")).strip()
    note = str(b.get("note", "")).strip()[:200]
    if not VERSION_RE.match(ver):
        return err(400, "bad_request", "Version: letters, numbers, . _ - only (max 32).")
    if db().execute("SELECT 1 FROM app_versions WHERE app_id=? AND version=?", (aid, ver)).fetchone():
        return err(409, "exists", "That version already exists.")
    t = now()
    db().execute("INSERT INTO app_versions(app_id,version,note,created_at) VALUES(?,?,?,?)", (aid, ver, note, t))
    db().execute(
        "INSERT INTO fields(app_id,version,name,value,public,updated_at) "
        "SELECT app_id,?,name,value,public,? FROM fields WHERE app_id=? AND version=?",
        (ver, t, aid, a["current_version"]),
    )
    db().execute("UPDATE apps SET current_version=? WHERE id=?", (ver, aid))
    db().commit()
    a = db().execute("SELECT * FROM apps WHERE id=?", (aid,)).fetchone()
    return jsonify({"ok": True, "app": app_json(a)}), 201


# ---- fields
def field_json(a, f):
    return {"name": f["name"], "value": f["value"], "public": bool(f["public"]), "version": f["version"],
            "updated_at": f["updated_at"], "url": field_url(a["slug"], f["version"], f["name"])}


@app.get("/api/v1/admin/apps/<int:aid>/versions/<version>/fields")
@admin_only
def fields_list(aid, version):
    a = db().execute("SELECT * FROM apps WHERE id=?", (aid,)).fetchone()
    if not a:
        return err(404, "not_found", "Unknown app.")
    ver = a["current_version"] if version == "latest" else version
    rows = db().execute("SELECT * FROM fields WHERE app_id=? AND version=? ORDER BY name", (aid, ver)).fetchall()
    return jsonify({"ok": True, "version": ver, "editable": ver == a["current_version"],
                    "fields": [field_json(a, f) for f in rows]})


@app.put("/api/v1/admin/apps/<int:aid>/fields/<name>")
@admin_only
def fields_put(aid, name):
    """Add or update a field in the CURRENT version. Body: {"value": "...", "public": false}"""
    a = db().execute("SELECT * FROM apps WHERE id=?", (aid,)).fetchone()
    if not a:
        return err(404, "not_found", "Unknown app.")
    if not FIELD_RE.match(name):
        return err(400, "bad_request", "Field name: letters, numbers, _ and - only (max 64).")
    b = body()
    value = str(b.get("value", ""))
    if not value or len(value) > 8192:
        return err(400, "bad_request", "Value is required (max 8192 characters).")
    db().execute(
        "INSERT INTO fields(app_id,version,name,value,public,updated_at) VALUES(?,?,?,?,?,?) "
        "ON CONFLICT(app_id,version,name) DO UPDATE SET value=excluded.value, public=excluded.public, updated_at=excluded.updated_at",
        (aid, a["current_version"], name, value, 1 if b.get("public") else 0, now()),
    )
    db().commit()
    f = db().execute("SELECT * FROM fields WHERE app_id=? AND version=? AND name=?", (aid, a["current_version"], name)).fetchone()
    return jsonify({"ok": True, "field": field_json(a, f)})


@app.delete("/api/v1/admin/apps/<int:aid>/fields/<name>")
@admin_only
def fields_delete(aid, name):
    a = db().execute("SELECT * FROM apps WHERE id=?", (aid,)).fetchone()
    if not a:
        return err(404, "not_found", "Unknown app.")
    db().execute("DELETE FROM fields WHERE app_id=? AND version=? AND name=?", (aid, a["current_version"], name))
    db().commit()
    return jsonify({"ok": True})


# ---- licenses
@app.get("/api/v1/admin/licenses")
@admin_only
def licenses_list():
    rows = db().execute("SELECT * FROM licenses ORDER BY id DESC").fetchall()
    return jsonify({"ok": True, "licenses": [license_json(r) for r in rows]})


@app.post("/api/v1/admin/licenses/generate")
@admin_only
def licenses_generate():
    """
    Body: {"app_id": 1,                      (optional; the app's current version is picked automatically)
           "plan_id": 2,                    (or) "custom": {"value": 3, "unit": "days"},
           "hwid_mode": "hwid" | "open",
           "quantity": 1-50, "note": "..."}
    """
    b = body()
    hwid_mode = b.get("hwid_mode", "hwid")
    if hwid_mode not in ("hwid", "open"):
        return err(400, "bad_request", "hwid_mode must be 'hwid' or 'open'.")
    try:
        qty = max(1, min(50, int(b.get("quantity", 1))))
    except (TypeError, ValueError):
        return err(400, "bad_request", "quantity must be a number.")
    app_id, app_name, version = None, "", ""
    if b.get("app_id") not in (None, ""):
        a = db().execute("SELECT * FROM apps WHERE id=?", (b.get("app_id"),)).fetchone()
        if not a:
            return err(404, "not_found", "Unknown app.")
        app_id, app_name, version = a["id"], a["name"], a["current_version"]
    if b.get("plan_id") not in (None, ""):
        p = db().execute("SELECT * FROM plans WHERE id=?", (b.get("plan_id"),)).fetchone()
        if not p:
            return err(404, "not_found", "Unknown plan.")
        plan_name, dv, du = p["name"], p["value"], p["unit"]
    else:
        c = b.get("custom") if isinstance(b.get("custom"), dict) else {}
        try:
            dv, du = clean_duration(c.get("value", 1), c.get("unit"))
        except ValueError as ex:
            return err(400, "bad_request", "Pick a plan_id or a valid custom duration: " + str(ex))
        plan_name = "Custom"
    note = str(b.get("note", "")).strip()[:200]
    made = []
    for _ in range(qty):
        for _try in range(5):
            key = gen_key()
            try:
                cur = db().execute(
                    "INSERT INTO licenses(key,app_id,app_name,version,plan_name,dur_value,dur_unit,hwid_mode,created_at,note) "
                    "VALUES(?,?,?,?,?,?,?,?,?,?)",
                    (key, app_id, app_name, version, plan_name, dv, du, hwid_mode, now(), note),
                )
                made.append(cur.lastrowid)
                break
            except sqlite3.IntegrityError:
                continue
    db().commit()
    rows = db().execute(f"SELECT * FROM licenses WHERE id IN ({','.join('?' * len(made))})", made).fetchall() if made else []
    return jsonify({"ok": True, "licenses": [license_json(r) for r in rows]}), 201


def _license_action(lid, sql, args=()):
    cur = db().execute(sql, (*args, lid))
    db().commit()
    if not cur.rowcount:
        return err(404, "not_found", "Unknown license.")
    return jsonify({"ok": True})


@app.post("/api/v1/admin/licenses/<int:lid>/revoke")
@admin_only
def licenses_revoke(lid):
    return _license_action(lid, "UPDATE licenses SET status='revoked' WHERE id=?")


@app.post("/api/v1/admin/licenses/<int:lid>/restore")
@admin_only
def licenses_restore(lid):
    return _license_action(lid, "UPDATE licenses SET status='ok' WHERE id=?")


@app.post("/api/v1/admin/licenses/<int:lid>/reset-hwid")
@admin_only
def licenses_reset_hwid(lid):
    return _license_action(lid, "UPDATE licenses SET hwid='' WHERE id=?")


@app.delete("/api/v1/admin/licenses/<int:lid>")
@admin_only
def licenses_delete(lid):
    return _license_action(lid, "DELETE FROM licenses WHERE id=?")


# ---- Discord: many channels (one webhook each, stored here and never sent to browsers)
WEBHOOK_RE = re.compile(r"^https://(?:(?:ptb|canary)\.)?(?:discord|discordapp)\.com/api(?:/v\d+)?/webhooks/(\d{5,25})/([A-Za-z0-9_-]{20,120})$")


def get_setting(key, default=""):
    row = db().execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    return row["value"] if row else default


def set_setting(key, value):
    db().execute("INSERT INTO settings(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, value))


def route_ids(key):
    try:
        v = json.loads(get_setting(key, "[]"))
    except ValueError:
        v = []
    return [x for x in v if isinstance(x, int)] if isinstance(v, list) else []


def channel_json(r):
    m = WEBHOOK_RE.match(r["webhook_url"])
    return {"id": r["id"], "name": r["name"], "masked": f"webhooks/{m.group(1)}/••••{m.group(2)[-4:]}" if m else ""}


def discord_status():
    rows = db().execute("SELECT * FROM discord_channels ORDER BY name COLLATE NOCASE").fetchall()
    ids = {r["id"] for r in rows}
    return {
        "configured": bool(rows),
        "channels": [channel_json(r) for r in rows],
        "routes": {
            "new_item": [i for i in route_ids("discord_route_new") if i in ids],
            "price_change": [i for i in route_ids("discord_route_price") if i in ids],
        },
        "notify_new_item": get_setting("discord_notify_new", "1") == "1",
        "notify_price_change": get_setting("discord_notify_price", "1") == "1",
    }


def discord_post(url, payload):
    """Send a message to one webhook. Returns (ok, message)."""
    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json", "User-Agent": "DiscordBot (https://nighthawks-cx.github.io, 1.0)"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=10):
            return True, "sent"
    except urllib.error.HTTPError as e:
        if e.code in (401, 404):
            return False, "that webhook no longer exists"
        if e.code == 429:
            return False, "Discord rate limit hit, try again in a few seconds"
        return False, f"Discord returned error {e.code}"
    except Exception as e:  # network / proxy / whitelist problems
        return False, "could not reach Discord (" + type(e).__name__ + ")"


def send_to_channels(ids, payload):
    """Post to several channels. Returns (all_ok, summary_message, sent_count)."""
    sent, failed = 0, []
    for cid in ids:
        r = db().execute("SELECT * FROM discord_channels WHERE id=?", (cid,)).fetchone()
        if not r:
            continue
        ok, msg = discord_post(r["webhook_url"], payload)
        if ok:
            sent += 1
        else:
            failed.append(f"{r['name']}: {msg}")
    return (not failed), "; ".join(failed), sent


def clean_ids(value):
    """Validate a list of existing channel ids. Returns list or None when invalid."""
    if not isinstance(value, list):
        return None
    out = []
    for x in value:
        if not isinstance(x, int) or isinstance(x, bool):
            return None
        if db().execute("SELECT 1 FROM discord_channels WHERE id=?", (x,)).fetchone() and x not in out:
            out.append(x)
    return out


def short(v, n):
    return str(v or "").strip()[:n]


def price_text(fjd, usdt):
    parts = []
    if short(fjd, 20):
        parts.append("FJD " + short(fjd, 20))
    if short(usdt, 20):
        parts.append(short(usdt, 20) + " USDT")
    return " / ".join(parts) or "—"


def num(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def build_embed(b):
    event = b.get("event")
    title = short(b.get("title"), 200) or "Untitled"
    store_url = short(b.get("store_url"), 300)
    emb = {"footer": {"text": "Nighthawks Corporation"}, "timestamp": datetime.now(timezone.utc).isoformat()}
    if store_url.startswith("https://"):
        emb["url"] = store_url
    if event == "new_item":
        emb.update(title="🆕 New in the store: " + title, color=0x8B5CF6)
        desc = short(b.get("description"), 300)
        if desc:
            emb["description"] = desc
        paid = b.get("type") == "sale"
        emb["fields"] = [
            {"name": "Category", "value": short(b.get("category"), 40) or "—", "inline": True},
            {"name": "Price" if paid else "Access", "value": price_text(b.get("price_fjd"), b.get("price_usdt")) if paid else "Free download", "inline": True},
        ]
        if paid and b.get("trial"):
            emb["fields"].append({"name": "Free trial", "value": "Available, ask in our Discord", "inline": True})
    else:  # price_change
        new_f, old_f = num(b.get("price_fjd")), num(b.get("old_fjd"))
        if new_f is not None and old_f is not None and new_f < old_f:
            emb.update(title="🔻 Price drop: " + title, color=0x34D399)
        elif new_f is not None and old_f is not None and new_f > old_f:
            emb.update(title="🔺 Price update: " + title, color=0xF59E0B)
        else:
            emb.update(title="💸 Price update: " + title, color=0xF59E0B)
        emb["fields"] = [
            {"name": "Before", "value": price_text(b.get("old_fjd"), b.get("old_usdt")), "inline": True},
            {"name": "Now", "value": price_text(b.get("price_fjd"), b.get("price_usdt")), "inline": True},
        ]
    return emb


@app.get("/api/v1/admin/discord")
@admin_only
def discord_get():
    return jsonify({"ok": True, **discord_status()})


@app.put("/api/v1/admin/discord")
@admin_only
def discord_put():
    """Routing + toggles. Body (all optional): {"notify_new_item": bool, "notify_price_change": bool,
    "route_new_item": [channel ids], "route_price_change": [channel ids]}"""
    b = body()
    if "notify_new_item" in b:
        set_setting("discord_notify_new", "1" if b.get("notify_new_item") else "0")
    if "notify_price_change" in b:
        set_setting("discord_notify_price", "1" if b.get("notify_price_change") else "0")
    for field, key in (("route_new_item", "discord_route_new"), ("route_price_change", "discord_route_price")):
        if field in b:
            ids = clean_ids(b.get(field))
            if ids is None:
                return err(400, "bad_request", field + " must be a list of channel ids.")
            set_setting(key, json.dumps(ids))
    db().commit()
    return jsonify({"ok": True, **discord_status()})


def _channel_fields(b, creating):
    name = short(b.get("name"), 40)
    url = str(b.get("webhook_url") or "").strip()
    if (creating or "name" in b) and not name:
        return None, "Give the channel a name."
    if (creating or url) and not WEBHOOK_RE.match(url):
        return None, "That does not look like a Discord webhook URL (https://discord.com/api/webhooks/ID/TOKEN)."
    return (name, url), None


@app.post("/api/v1/admin/discord/channels")
@admin_only
def channels_create():
    vals, e = _channel_fields(body(), True)
    if e:
        return err(400, "bad_request", e)
    first = not db().execute("SELECT 1 FROM discord_channels LIMIT 1").fetchone()
    try:
        cur = db().execute("INSERT INTO discord_channels(name,webhook_url,created_at) VALUES(?,?,?)", (vals[0], vals[1], now()))
    except sqlite3.IntegrityError:
        return err(409, "exists", "A channel with that name already exists.")
    if first:  # the first channel receives the automatic posts until you change it
        set_setting("discord_route_new", json.dumps([cur.lastrowid]))
        set_setting("discord_route_price", json.dumps([cur.lastrowid]))
    db().commit()
    return jsonify({"ok": True, **discord_status()}), 201


@app.put("/api/v1/admin/discord/channels/<int:cid>")
@admin_only
def channels_update(cid):
    if not db().execute("SELECT 1 FROM discord_channels WHERE id=?", (cid,)).fetchone():
        return err(404, "not_found", "Unknown channel.")
    b = body()
    vals, e = _channel_fields(b, False)
    if e:
        return err(400, "bad_request", e)
    try:
        if "name" in b:
            db().execute("UPDATE discord_channels SET name=? WHERE id=?", (vals[0], cid))
        if vals[1]:
            db().execute("UPDATE discord_channels SET webhook_url=? WHERE id=?", (vals[1], cid))
    except sqlite3.IntegrityError:
        return err(409, "exists", "A channel with that name already exists.")
    db().commit()
    return jsonify({"ok": True, **discord_status()})


@app.delete("/api/v1/admin/discord/channels/<int:cid>")
@admin_only
def channels_delete(cid):
    db().execute("DELETE FROM discord_channels WHERE id=?", (cid,))
    for key in ("discord_route_new", "discord_route_price"):
        set_setting(key, json.dumps([i for i in route_ids(key) if i != cid]))
    db().commit()
    return jsonify({"ok": True, **discord_status()})


@app.post("/api/v1/admin/discord/channels/<int:cid>/test")
@admin_only
def channels_test(cid):
    ok, msg, sent = send_to_channels([cid], {"allowed_mentions": {"parse": []}, "embeds": [
        {"title": "Nighthawks test", "description": "Connection works ✅", "color": 0x8B5CF6}]})
    if not ok or not sent:
        return err(502, "discord_error", msg or "Unknown channel.")
    return jsonify({"ok": True})


@app.post("/api/v1/admin/discord/send")
@admin_only
def discord_send():
    """Drop a message. Body: {"channel_ids": [1, 2], "content": "...", "title": "optional", "allow_mentions": false}"""
    b = body()
    ids = clean_ids(b.get("channel_ids"))
    if not ids:
        return err(400, "bad_request", "Pick at least one channel.")
    content = str(b.get("content") or "").strip()
    title = short(b.get("title"), 200)
    if not content and not title:
        return err(400, "bad_request", "Write a message first.")
    if len(content) > 2000:
        return err(400, "bad_request", "Discord messages are limited to 2000 characters.")
    payload = {"allowed_mentions": {"parse": ["everyone", "roles", "users"] if b.get("allow_mentions") else []}}
    if title:
        payload["embeds"] = [{"title": title, "description": content[:4000], "color": 0x8B5CF6,
                              "footer": {"text": "Nighthawks Corporation"}}]
    else:
        payload["content"] = content
    ok, msg, sent = send_to_channels(ids, payload)
    if not ok:
        return err(502, "discord_error", "Failed for " + msg + (f" (sent to {sent} other channel(s))" if sent else ""))
    return jsonify({"ok": True, "sent": sent})


@app.post("/api/v1/admin/discord/notify")
@admin_only
def discord_notify():
    """Automatic store announcements, routed to the channels chosen for that event.
    Body: {"event": "new_item"|"price_change", title, category, type, price_fjd, ...}"""
    b = body()
    event = b.get("event")
    if event not in ("new_item", "price_change"):
        return err(400, "bad_request", "event must be new_item or price_change.")
    st = discord_status()
    if not (st["notify_new_item"] if event == "new_item" else st["notify_price_change"]):
        return jsonify({"ok": True, "sent": 0, "reason": "disabled"})
    ids = st["routes"][event]
    if not ids:
        return jsonify({"ok": True, "sent": 0, "reason": "no_channel"})
    ok, msg, sent = send_to_channels(ids, {"allowed_mentions": {"parse": []}, "embeds": [build_embed(b)]})
    if not ok:
        return err(502, "discord_error", "Failed for " + msg)
    return jsonify({"ok": True, "sent": sent})


# --------------------------------------------------------------------------- CLI
if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "run"
    if cmd == "init":
        init_db()
        if load_admin_token():
            print("Database ready. An admin token already exists (env var or admin_token.txt).")
        else:
            tok = secrets.token_urlsafe(32)
            with open(TOKEN_FILE, "w", encoding="utf-8") as fh:
                fh.write(tok)
            try:
                os.chmod(TOKEN_FILE, 0o600)
            except OSError:
                pass
            print("Database ready.\nYour ADMIN TOKEN (saved to admin_token.txt, keep it secret):\n\n  " + tok + "\n")
    elif cmd == "run":
        app.run(debug=False, port=5000)  # local testing only; PythonAnywhere uses the WSGI file
    else:
        print("Usage: python3 app.py [init|run]")
