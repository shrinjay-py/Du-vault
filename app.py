import os
import io
import re
import hmac
import time
import zlib
import html
import hashlib
import json
import difflib
import unicodedata
import threading
from concurrent.futures import ThreadPoolExecutor
import urllib.parse
from collections import OrderedDict
import libsql_experimental as sqlite3
from fastapi import FastAPI, UploadFile, File, Form, Response, Request
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse
from fastapi.middleware.gzip import GZipMiddleware
from starlette.concurrency import run_in_threadpool

app = FastAPI(title="DU PYQ Vault")
app.add_middleware(GZipMiddleware, minimum_size=1000)

TURSO_DB_URL = os.getenv("TURSO_DB_URL")
TURSO_AUTH_TOKEN = os.getenv("TURSO_AUTH_TOKEN")

# ───────────────────────── ADMIN AUTH (signed, expiring cookie) ─────────────────────────
ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD")
if not ADMIN_PASSWORD:
    raise RuntimeError("ADMIN_PASSWORD env var is not set")

# Optional separate secret; falls back to the admin password
SESSION_SECRET = (os.getenv("SESSION_SECRET") or ADMIN_PASSWORD).encode()
SESSION_TTL = 60 * 60 * 12  # 12 hours
COOKIE_SECURE = os.getenv("COOKIE_SECURE", "1") == "1"  # set COOKIE_SECURE=0 only for local http testing


def _sign(value: str) -> str:
    return hmac.new(SESSION_SECRET, value.encode(), hashlib.sha256).hexdigest()


def make_session_token() -> str:
    expires = str(int(time.time()) + SESSION_TTL)
    return f"{expires}.{_sign(expires)}"


def is_admin(request: Request) -> bool:
    token = request.cookies.get("du_admin_session", "")
    try:
        expires, sig = token.split(".")
        return hmac.compare_digest(sig, _sign(expires)) and int(expires) > time.time()
    except Exception:
        return False


# ───────────────────────── SMALL HELPERS ─────────────────────────
def esc(value) -> str:
    """HTML-escape anything that goes into the page."""
    return html.escape(str(value if value is not None else ""), quote=True)


def urlq(value: str) -> str:
    return urllib.parse.quote_plus(value)


def safe_http_url(url: str) -> bool:
    p = urllib.parse.urlparse(url.strip())
    return p.scheme in ("http", "https") and bool(p.netloc)


def back_url(request: Request) -> str:
    """Return to the page the admin was on (same site only)."""
    ref = request.headers.get("referer", "")
    if ref:
        p = urllib.parse.urlparse(ref)
        if p.netloc == request.headers.get("host", ""):
            return (p.path or "/") + (f"?{p.query}" if p.query else "")
    return "/"


# ───────────────────────── SPEED: IN-MEMORY CACHES ─────────────────────────
MAX_PDF_BYTES = 25 * 1024 * 1024  # one PDF; protects the server's memory and the database
CACHE_TTL = 120  # seconds; every admin change (any POST) also clears the cache immediately
_CACHE = {}
_DIRTY = True
_LOADED_AT = 0.0
_LOCK = threading.Lock()
_BLOBS = OrderedDict()  # recently opened PDFs / background image, kept in RAM
_BLOB_BYTES = 0
BLOB_CACHE_LIMIT = 48 * 1024 * 1024


def cache_clear():
    global _DIRTY, _BLOB_BYTES
    with _LOCK:
        _CACHE.clear()
        _BLOBS.clear()
        _BLOB_BYTES = 0
        _DIRTY = True


def cached(key, fn):
    hit = _CACHE.get(key)
    if hit and time.time() - hit[0] < CACHE_TTL:
        return hit[1]
    val = fn()
    with _LOCK:
        if len(_CACHE) > 400:
            _CACHE.clear()
        _CACHE[key] = (time.time(), val)
    return val


# ───────────────────────── SNAPSHOT: the whole (small) catalogue lives in memory ─────────────────────────
# Every database question costs a full network round trip (about half a second to a far-away Turso region).
# So the app reads courses, subjects and the paper list (without the PDF files) ONCE, keeps them in memory,
# and refreshes them in the background. Page views then need no database at all.
SNAP = {"ready": False, "at": 0.0, "sig": None, "papers": [], "bg": None}
_SNAP_LOCK = threading.Lock()
_EXEC = ThreadPoolExecutor(max_workers=4, thread_name_prefix="snap")
SNAP_TTL = 45  # seconds between quiet background refreshes (admin changes refresh immediately)


def _q(sql, params=()):
    """Read helper: reuses this thread's connection; if it had gone stale, reconnects and tries once more."""
    for attempt in (1, 2):
        conn = get_db()
        try:
            cur = conn.cursor()
            cur.execute(sql, params)
            return cur.fetchall()
        except Exception:
            _tls.raw = None  # drop the bad connection
            if attempt == 2:
                raise
        finally:
            conn.close()


class _SharedPool:
    """Lets the snapshot refresh reuse the same 4 worker threads (and so their open connections) every time."""

    def __enter__(self):
        return _EXEC

    def __exit__(self, *exc):
        return False


def apply_courses(rows):
    COURSE_LIST[:] = [
        {"key": k, "name": n, "label": l or n, "icon": i or "folder", "color": col or ""}
        for k, n, l, i, col in rows
    ]


def apply_curriculum(rows):
    fresh, style = {}, {}
    for c, sm, n, ic, col in rows:
        fresh.setdefault(c, {}).setdefault(sm, []).append(n)
        if ic or col:
            style[(c, n)] = (ic or "", col or "")
    CURRICULUM.clear()
    CURRICULUM.update(fresh)
    SUBJECT_STYLE.clear()
    SUBJECT_STYLE.update(style)


def refresh_snapshot(block=False, force=False):
    """Reads everything in parallel (4 queries at once = one round-trip of waiting)."""
    if not _SNAP_LOCK.acquire(blocking=block):
        return False  # someone else is already refreshing
    try:
        if not force and SNAP["ready"] and time.time() - SNAP["at"] < 5:
            return True
        with _SharedPool() as ex:
            futs = [
                ex.submit(_q, "SELECT key, name, label, icon, color FROM du_courses ORDER BY position, id"),
                ex.submit(_q, "SELECT course, semester, name, icon, color FROM du_subjects ORDER BY id"),
                ex.submit(_q, "SELECT id, title, course, semester, year, type, url_or_name, file_size, subject FROM du_resources"),
                ex.submit(_q, "SELECT version, opacity FROM du_background WHERE id = 1"),
            ]
            courses, subjects, papers, bg = [f.result() for f in futs]
        sig = hash(repr((courses, subjects, papers, bg)))
        changed = sig != SNAP["sig"]
        if changed:
            apply_courses(courses)
            apply_curriculum(subjects)
            SNAP["papers"] = [tuple(r) for r in papers]
            SNAP["bg"] = tuple(bg[0]) if bg else None
            SNAP["sig"] = sig
        SNAP["at"] = time.time()
        SNAP["ready"] = True
        if changed:
            with _LOCK:
                _CACHE.clear()
        return True
    finally:
        _SNAP_LOCK.release()


def _bg_refresh():
    try:
        refresh_snapshot(block=False)
    except Exception as exc:  # keep serving the last good copy if the database hiccups
        print(f"snapshot refresh failed: {exc}", flush=True)


def ensure_snapshot():
    if not SNAP["ready"]:
        refresh_snapshot(block=True)  # very first load only (a startup thread normally has done this already)
    elif time.time() - SNAP["at"] > SNAP_TTL:
        threading.Thread(target=_bg_refresh, daemon=True).start()  # serve the current copy, refresh quietly


def snap_counts():
    out = {}
    for r in SNAP["papers"]:
        out[r[2]] = out.get(r[2], 0) + 1
    return out


def snap_subcounts(course):
    out = {}
    for r in SNAP["papers"]:
        if r[2] == course:
            k = (r[8] or UNSORTED, r[3])
            out[k] = out.get(k, 0) + 1
    return [(sub, sm, n) for (sub, sm), n in out.items()]


def snap_years(course, subject):
    return {r[4] for r in SNAP["papers"] if r[2] == course and (r[8] or UNSORTED) == subject and r[4]}


def snap_known_subjects():
    return {r[8] for r in SNAP["papers"] if r[8]}


def snap_records(course, subject, sem, year, q):
    ql = (q or "").lower()
    out = []
    for r in SNAP["papers"]:
        if course and r[2] != course:
            continue
        if subject and (r[8] or UNSORTED) != subject:
            continue
        if sem != "All Semesters" and not subject and r[3] != sem:
            continue
        if subject and year and year != "All Years" and (r[4] or "") != year:
            continue
        if ql and not any(ql in str(x or "").lower() for x in (r[1], r[2], r[4], r[8])):
            continue
        out.append(r)
    out.sort(key=lambda r: ((r[4] or ""), r[0]), reverse=True)  # newest year first, then newest upload
    return out


def parse_mb(text) -> int:
    try:
        return int(float(str(text).split()[0]) * 1048576)
    except Exception:
        return 0


def blob_get(key):
    with _LOCK:
        item = _BLOBS.get(key)
        if item is not None:
            _BLOBS.move_to_end(key)
        return item[0] if item else None


def blob_put(key, value, size):
    global _BLOB_BYTES
    if size > BLOB_CACHE_LIMIT // 2:
        return
    with _LOCK:
        _BLOBS[key] = (value, size)
        _BLOB_BYTES += size
        while _BLOB_BYTES > BLOB_CACHE_LIMIT and _BLOBS:
            _, (_, sz) = _BLOBS.popitem(last=False)
            _BLOB_BYTES -= sz


@app.middleware("http")
async def housekeeping(request: Request, call_next):
    start = time.perf_counter()
    # Only a logged-in admin's changes clear the caches (visitors can't force constant cache misses)
    write = request.method == "POST" and is_admin(request)
    if write:
        cache_clear()
    response = await call_next(request)
    if write:
        cache_clear()
        try:
            await run_in_threadpool(refresh_snapshot, True, True)  # so the admin sees the change on the next page
        except Exception as exc:
            print(f"snapshot refresh after write failed: {exc}", flush=True)
    ms = (time.perf_counter() - start) * 1000
    response.headers["Server-Timing"] = f"app;dur={ms:.0f}"  # visible in the browser's Network tab
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("X-Frame-Options", "DENY")
    response.headers.setdefault("Referrer-Policy", "same-origin")
    if ms > 1500:
        print(f"SLOW {request.method} {request.url.path} {ms:.0f}ms", flush=True)  # shows up in Render logs
    return response


def add_fresh(url: str) -> str:
    return url + ("&" if "?" in url else "?") + "fresh=1"


def _open_raw():
    if TURSO_DB_URL and TURSO_AUTH_TOKEN:
        return sqlite3.connect(TURSO_DB_URL, auth_token=TURSO_AUTH_TOKEN)
    return sqlite3.connect("du_pyq_vault.db")


# Opening a brand-new Turso connection costs about a second for its first question. So each worker thread keeps
# its connection open and reuses it (a connection is only ever used by the thread that created it).
_tls = threading.local()
IDLE_CHECK_SECONDS = 5  # after this much idle time, test the connection before trusting it


class PooledConn:
    """Behaves like a normal connection, but close() keeps the real one for the next request on this thread."""

    def __init__(self, raw):
        self._raw = raw

    def cursor(self):
        return self._raw.cursor()

    def commit(self):
        return self._raw.commit()

    def rollback(self):
        return self._raw.rollback()

    def close(self):
        try:
            if getattr(self._raw, "in_transaction", False):
                self._raw.rollback()  # never leave half-finished changes behind
        except Exception:
            _tls.raw = None
        _tls.in_use = False
        _tls.last = time.time()


def get_db():
    raw = getattr(_tls, "raw", None)
    if raw is not None:
        try:
            if getattr(_tls, "in_use", False):
                raw.rollback()  # the previous user crashed before closing: discard its unfinished work
            if time.time() - getattr(_tls, "last", 0) > IDLE_CHECK_SECONDS:
                cur = raw.cursor()
                cur.execute("SELECT 1")
                cur.fetchall()  # the server may have dropped an idle connection
        except Exception:
            raw = None
    if raw is None:
        raw = _open_raw()
    _tls.raw = raw
    _tls.in_use = True
    return PooledConn(raw)


# ───────────────────────── COURSES / SEMESTERS ─────────────────────────
COURSES = [
    "All Courses",
    "B.Sc (Hons) Botany",
    "B.Sc (Hons) Zoology",
    "B.Sc (prog) Life Sciences",
    "B.Sc (Hons) Chemistry",
    "B.Sc (prog) Physical science with Chemistry",
    "B.Sc (Hons) Mathematics",
    "B.Sc (Hons) Physics",
    "B.Sc (Hons) Computer Science",
    "B.Com (Hons)",
    "B.Com (Programme)",
    "B.A. (Hons) Economics",
    "B.A. (Hons) English",
    "B.A. (Hons) Political Science",
    "B.A. Programme",
    "B.Tech / CIC",
    "Postgraduate",
    "General / Other",
]

COURSE_TILES = [
    {"name": "B.Sc (Hons) Botany", "icon": "🌿", "label": "Botany"},
    {"name": "B.Sc (Hons) Zoology", "icon": "🦁", "label": "Zoology"},
    {"name": "B.Sc (prog) Life Sciences", "icon": "🧬", "label": "Life Science"},
    {"name": "B.Sc (Hons) Chemistry", "icon": "🧪", "label": "Chemistry"},
    {"name": "B.Sc (Hons) Mathematics", "icon": "📐", "label": "Mathematics"},
    {"name": "B.Sc (Hons) Physics", "icon": "⚛️", "label": "Physics"},
    {"name": "B.Sc (Hons) Computer Science", "icon": "💻", "label": "Computer Sci"},
    {"name": "B.Com (Hons)", "icon": "📊", "label": "B.Com (H)"},
    {"name": "B.A. (Hons) Economics", "icon": "📈", "label": "Economics"},
    {"name": "B.Sc (prog) Physical science with Chemistry", "icon": "🧲", "label": "Physical science with chem"},
]

SEMESTERS = ["All Semesters", "Sem 1", "Sem 2", "Sem 3", "Sem 4", "Sem 5", "Sem 6"]
UNSORTED = "Unsorted"

# Modern line-icon set (24x24, drawn as inline SVG). Admin can pick any of these, or type any emoji.
ICON_LIBRARY = {
    "leaf": '<path d="M11 20A7 7 0 0 1 9.8 6.1C15.5 5 17 4.48 19 2c1 2 2 4.18 2 8 0 5.5-4.78 10-10 10Z"/><path d="M2 21c0-3 1.85-5.36 5.08-6C9.5 14.52 12 13 13 12"/>',
    "sprout": '<path d="M7 20h10"/><path d="M10 20c5.5-2.5.8-6.4 3-10"/><path d="M9.5 9.4c1.1.8 1.8 2.2 2.3 3.7-2 .4-3.5.4-4.8-.3-1.2-.6-2.3-1.9-3-4.2 2.8-.5 4.4 0 5.5.8z"/><path d="M14.1 6a7 7 0 0 0-1.1 4c1.9-.1 3.3-.6 4.3-1.4 1-1 1.6-2.3 1.7-4.6-2.7.1-4 1-4.9 2z"/>',
    "flower": '<circle cx="12" cy="7" r="3"/><circle cx="17" cy="12" r="3"/><circle cx="12" cy="17" r="3"/><circle cx="7" cy="12" r="3"/><circle cx="12" cy="12" r="1.2"/>',
    "tree": '<path d="M12 22v-5"/><path d="M12 17a5 5 0 0 0 5-5c0-1.2-.4-2.2-1-3a4 4 0 0 0-3.5-5.5A4 4 0 0 0 12 2a4 4 0 0 0-.5 1.5A4 4 0 0 0 8 9c-.6.8-1 1.8-1 3a5 5 0 0 0 5 5z"/>',
    "dna": '<path d="M8 3c0 5 8 5 8 9s-8 4-8 9"/><path d="M16 3c0 5-8 5-8 9s8 4 8 9"/><path d="M9.2 6.5h5.6M8.4 12h7.2M9.2 17.5h5.6"/>',
    "microscope": '<path d="M6 18h8"/><path d="M3 22h18"/><path d="M14 22a7 7 0 1 0 0-14h-1"/><path d="M9 14h2"/><path d="M9 12a2 2 0 0 1-2-2V6h6v4a2 2 0 0 1-2 2Z"/><path d="M12 6V3a1 1 0 0 0-1-1H9a1 1 0 0 0-1 1v3"/>',
    "flask": '<path d="M10 2v7.527a2 2 0 0 1-.211.896L4.72 20.55a1 1 0 0 0 .9 1.45h12.76a1 1 0 0 0 .9-1.45l-5.069-10.127A2 2 0 0 1 14 9.527V2"/><path d="M8.5 2h7"/><path d="M7 16h10"/>',
    "atom": '<circle cx="12" cy="12" r="1"/><path d="M20.2 20.2c2.04-2.03.02-7.36-4.5-11.9-4.54-4.52-9.87-6.54-11.9-4.5-2.04 2.03-.02 7.36 4.5 11.9 4.54 4.52 9.87 6.54 11.9 4.5Z"/><path d="M15.7 15.7c4.52-4.54 6.54-9.87 4.5-11.9-2.03-2.04-7.36-.02-11.9 4.5-4.52 4.54-6.54 9.87-4.5 11.9 2.03 2.04 7.36.02 11.9-4.5Z"/>',
    "sigma": '<path d="M18 7V5a1 1 0 0 0-1-1H6.5a.5.5 0 0 0-.4.8l4.5 6a2 2 0 0 1 0 2.4l-4.5 6a.5.5 0 0 0 .4.8H17a1 1 0 0 0 1-1v-2"/>',
    "infinity": '<path d="M6 16c5 0 7-8 12-8a4 4 0 0 1 0 8c-5 0-7-8-12-8a4 4 0 1 0 0 8"/>',
    "shapes": '<path d="M8.3 10a.7.7 0 0 1-.626-1.079L11.4 3a.7.7 0 0 1 1.198-.043L16.3 8.9a.7.7 0 0 1-.572 1.1Z"/><rect x="3" y="14" width="7" height="7" rx="1"/><circle cx="17.5" cy="17.5" r="3.5"/>',
    "code": '<path d="m16 18 6-6-6-6"/><path d="m8 6-6 6 6 6"/>',
    "monitor": '<rect width="20" height="14" x="2" y="3" rx="2"/><path d="M8 21h8"/><path d="M12 17v4"/>',
    "cpu": '<rect width="16" height="16" x="4" y="4" rx="2"/><rect width="6" height="6" x="9" y="9" rx="1"/><path d="M15 2v2M15 20v2M2 15h2M2 9h2M20 15h2M20 9h2M9 2v2M9 20v2"/>',
    "database": '<ellipse cx="12" cy="5" rx="9" ry="3"/><path d="M3 5v14a9 3 0 0 0 18 0V5"/><path d="M3 12a9 3 0 0 0 18 0"/>',
    "network": '<rect x="16" y="16" width="6" height="6" rx="1"/><rect x="2" y="16" width="6" height="6" rx="1"/><rect x="9" y="2" width="6" height="6" rx="1"/><path d="M5 16v-3a1 1 0 0 1 1-1h12a1 1 0 0 1 1 1v3"/><path d="M12 12V8"/>',
    "chart": '<path d="M3 3v18h18"/><path d="M18 17V9"/><path d="M13 17V5"/><path d="M8 17v-3"/>',
    "trending": '<path d="M22 7 13.5 15.5 8.5 10.5 2 17"/><path d="M16 7h6v6"/>',
    "pie": '<path d="M21 12c.552 0 1.005-.449.95-.998a10 10 0 0 0-8.953-8.951c-.55-.055-.998.398-.998.95v8a1 1 0 0 0 1 1z"/><path d="M21.21 15.89A10 10 0 1 1 8 2.83"/>',
    "dice": '<rect width="18" height="18" x="3" y="3" rx="2"/><path d="M8 8h.01M16 8h.01M8 16h.01M16 16h.01M12 12h.01"/>',
    "globe": '<circle cx="12" cy="12" r="10"/><path d="M12 2a14.5 14.5 0 0 0 0 20 14.5 14.5 0 0 0 0-20"/><path d="M2 12h20"/>',
    "droplet": '<path d="M12 22a7 7 0 0 0 7-7c0-2-1-3.9-3-5.5s-3.5-4-4-6.5c-.5 2.5-2 4.9-4 6.5C6 11.1 5 13 5 15a7 7 0 0 0 7 7z"/>',
    "flame": '<path d="M8.5 14.5A2.5 2.5 0 0 0 11 12c0-1.38-.5-2-1-3-1.072-2.143-.224-4.054 2-6 .5 2.5 2 4.9 4 6.5 2 1.6 3 3.5 3 5.5a7 7 0 1 1-14 0c0-1.153.433-2.294 1-3a2.5 2.5 0 0 0 2.5 2.5z"/>',
    "zap": '<path d="M4 14a1 1 0 0 1-.78-1.63l9.9-10.2a.5.5 0 0 1 .86.46l-1.92 6.02A1 1 0 0 0 13 10h7a1 1 0 0 1 .78 1.63l-9.9 10.2a.5.5 0 0 1-.86-.46l1.92-6.02A1 1 0 0 0 11 14z"/>',
    "magnet": '<path d="m6 15-4-4 6.75-6.77a7.79 7.79 0 0 1 11 11L13 22l-4-4 6.39-6.36a2.14 2.14 0 0 0-3-3L6 15"/><path d="m5 8 4 4"/><path d="m12 15 4 4"/>',
    "waves": '<path d="M2 6c.6.5 1.2 1 2.5 1C7 7 7 5 9.5 5c2.6 0 2.4 2 5 2 2.5 0 2.5-2 5-2 1.3 0 1.9.5 2.5 1"/><path d="M2 12c.6.5 1.2 1 2.5 1 2.5 0 2.5-2 5-2 2.6 0 2.4 2 5 2 2.5 0 2.5-2 5-2 1.3 0 1.9.5 2.5 1"/><path d="M2 18c.6.5 1.2 1 2.5 1 2.5 0 2.5-2 5-2 2.6 0 2.4 2 5 2 2.5 0 2.5-2 5-2 1.3 0 1.9.5 2.5 1"/>',
    "telescope": '<path d="M3 11 18 5l2 5-15 6z"/><path d="M9 16l-2 6"/><path d="M12 15l2 7"/>',
    "bug": '<path d="m8 2 1.88 1.88"/><path d="M14.12 3.88 16 2"/><path d="M9 7.13v-1a3.003 3.003 0 1 1 6 0v1"/><path d="M12 20c-3.3 0-6-2.7-6-6v-3a4 4 0 0 1 4-4h4a4 4 0 0 1 4 4v3c0 3.3-2.7 6-6 6"/><path d="M12 20v-9"/><path d="M6 13H2"/><path d="M22 13h-4"/>',
    "mushroom": '<path d="M3 12a9 8 0 0 1 18 0Z"/><path d="M9 12v5a3 3 0 0 0 6 0v-5"/><path d="M8 8.5h.01M13 7h.01M16 10h.01"/>',
    "fish": '<path d="M2 12c3-6 9-8 14-5l6 5-6 5c-5 3-11 1-14-5Z"/><path d="M16 11h.01"/>',
    "paw": '<circle cx="11" cy="4" r="2"/><circle cx="18" cy="8" r="2"/><circle cx="20" cy="16" r="2"/><path d="M9 10a5 5 0 0 1 5 5v3.5a3.5 3.5 0 0 1-6.84 1.045Q6.52 17.48 4.46 16.84A3.5 3.5 0 0 1 5.5 10Z"/>',
    "bulb": '<path d="M15 14c.2-1 .7-1.7 1.5-2.5 1-.9 1.5-2.2 1.5-3.5A6 6 0 0 0 6 8c0 1 .2 2.2 1.5 3.5.7.7 1.3 1.5 1.5 2.5"/><path d="M9 18h6"/><path d="M10 22h4"/>',
    "layers": '<path d="m12.83 2.18a2 2 0 0 0-1.66 0L2.6 6.08a1 1 0 0 0 0 1.83l8.58 3.91a2 2 0 0 0 1.66 0l8.58-3.9a1 1 0 0 0 0-1.83Z"/><path d="m22 17.65-9.17 4.16a2 2 0 0 1-1.66 0L2 17.65"/><path d="m22 12.65-9.17 4.16a2 2 0 0 1-1.66 0L2 12.65"/>',
    "book": '<path d="M4 19.5v-15A2.5 2.5 0 0 1 6.5 2H19a1 1 0 0 1 1 1v18a1 1 0 0 1-1 1H6.5a1 1 0 0 1 0-5H20"/>',
    "book-open": '<path d="M12 7v14"/><path d="M3 18a1 1 0 0 1-1-1V4a1 1 0 0 1 1-1h5a4 4 0 0 1 4 4 4 4 0 0 1 4-4h5a1 1 0 0 1 1 1v13a1 1 0 0 1-1 1h-6a3 3 0 0 0-3 3 3 3 0 0 0-3-3z"/>',
    "file": '<path d="M15 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V7Z"/><path d="M14 2v4a2 2 0 0 0 2 2h4"/><path d="M10 9H8"/><path d="M16 13H8"/><path d="M16 17H8"/>',
    "scale": '<path d="m16 16 3-8 3 8c-.87.65-1.92 1-3 1s-2.13-.35-3-1Z"/><path d="m2 16 3-8 3 8c-.87.65-1.92 1-3 1s-2.13-.35-3-1Z"/><path d="M7 21h10"/><path d="M12 3v18"/><path d="M3 7h2c2 0 5-1 7-2 2 1 5 2 7 2h2"/>',
    "landmark": '<path d="M3 22h18"/><path d="M6 18v-7M10 18v-7M14 18v-7M18 18v-7"/><path d="M12 2 20 7H4z"/>',
    "wallet": '<path d="M19 7V4a1 1 0 0 0-1-1H5a2 2 0 0 0 0 4h15a1 1 0 0 1 1 1v4h-3a2 2 0 0 0 0 4h3a1 1 0 0 0 1-1v-2a1 1 0 0 0-1-1"/><path d="M3 5v14a2 2 0 0 0 2 2h15a1 1 0 0 0 1-1v-4"/>',
    "cap": '<path d="M21.42 10.922a1 1 0 0 0-.019-1.838L12.83 5.18a2 2 0 0 0-1.66 0L2.6 9.08a1 1 0 0 0 0 1.832l8.57 3.908a2 2 0 0 0 1.66 0z"/><path d="M22 10v6"/><path d="M6 12.5V16a6 3 0 0 0 12 0v-3.5"/>',
    "folder": '<path d="M20 20a2 2 0 0 0 2-2V8a2 2 0 0 0-2-2h-7.9a2 2 0 0 1-1.69-.9L9.6 3.9A2 2 0 0 0 7.93 3H4a2 2 0 0 0-2 2v13a2 2 0 0 0 2 2Z"/>',
}


def icon_svg(key: str, size: int = 30) -> str:
    """Inline SVG for a library icon; anything else (e.g. an emoji the admin typed) is shown as escaped text."""
    inner = ICON_LIBRARY.get(key or "")
    if inner is None:
        return esc(key)
    return (
        f'<svg viewBox="0 0 24 24" width="{size}" height="{size}" fill="none" stroke="currentColor" '
        f'stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">{inner}</svg>'
    )


# Courses are now stored in the database (du_courses). These are only the starting values on first run.
DEFAULT_COURSES = [  # (name, short label, icon)
    ("B.Sc (Hons) Botany", "Botany", "leaf"),
    ("B.Sc (Hons) Zoology", "Zoology", "paw"),
    ("B.Sc (prog) Life Sciences", "Life Science", "dna"),
    ("B.Sc (Hons) Chemistry", "Chemistry", "flask"),
    ("B.Sc (Hons) Mathematics", "Mathematics", "shapes"),
    ("B.Sc (Hons) Physics", "Physics", "atom"),
    ("B.Sc (Hons) Computer Science", "Computer Sci", "code"),
    ("B.Com (Hons)", "B.Com (H)", "wallet"),
    ("B.A. (Hons) Economics", "Economics", "trending"),
    ("B.Sc (prog) Physical science with Chemistry", "Physical science with chem", "magnet"),
    ("B.Com (Programme)", "B.Com (Prog)", "wallet"),
    ("B.A. (Hons) English", "English", "book-open"),
    ("B.A. (Hons) Political Science", "Pol. Science", "landmark"),
    ("B.A. Programme", "B.A. Prog", "cap"),
    ("B.Tech / CIC", "B.Tech / CIC", "cpu"),
    ("Postgraduate", "Postgraduate", "cap"),
    ("General / Other", "General / Other", "folder"),
]
GENERAL_KEY = "General / Other"
COURSE_LIST = []  # live list of dicts: key, name, label, icon, color


def course_names():
    return [c["name"] for c in COURSE_LIST]


def course_tile(name: str):
    return next((c for c in COURSE_LIST if c["name"] == name), None)

# ───────────────────────── SUBJECT CATALOGUE ─────────────────────────
# CURRICULUM is the live subject list, loaded from the du_subjects table (managed by the admin in the app).
# CURRICULUM_SEED is only used ONCE, to fill an empty du_subjects table on first start.
CURRICULUM = {}
SUBJECT_STYLE = {}  # (course, subject) -> (custom icon, colour index)
CURRICULUM_SEED = {
    "B.Sc (Hons) Botany": {
        "Sem 1": ["Biomolecules & Cell Biology", "Phycology & Microbiology"],
        "Sem 2": ["Mycology & Phytopathology", "Archegoniate"],
        "Sem 3": ["Anatomy of Angiosperms", "Economic Botany", "Genetics"],
        "Sem 4": ["Molecular Biology", "Plant Ecology & Phytogeography", "Plant Systematics"],
        "Sem 5": ["Reproductive Biology of Angiosperms", "Plant Physiology"],
        "Sem 6": ["Plant Metabolism", "Plant Biotechnology"],
    },
    "B.Sc (Hons) Zoology": {
        "Sem 1": ["Non-Chordates I", "Principles of Ecology"],
        "Sem 2": ["Non-Chordates II", "Cell Biology"],
        "Sem 3": ["Diversity of Chordates", "Animal Physiology: Controlling & Coordinating Systems", "Fundamentals of Biochemistry"],
        "Sem 4": ["Comparative Anatomy of Vertebrates", "Animal Physiology: Life Sustaining Systems", "Biochemistry of Metabolic Processes"],
        "Sem 5": ["Molecular Biology", "Principles of Genetics"],
        "Sem 6": ["Developmental Biology", "Evolutionary Biology"],
    },
    "B.Sc (prog) Life Sciences": {
        "Sem 1": ["Biodiversity (Microbes, Algae, Fungi & Archegoniate)", "Chemistry for Life Sciences"],
        "Sem 2": ["Plant Diversity & Ecology", "Animal Diversity"],
        "Sem 3": ["Cell Biology", "Biochemistry"],
        "Sem 4": ["Genetics", "Physiology"],
        "Sem 5": ["Molecular Biology", "Biotechnology"],
        "Sem 6": ["Developmental Biology", "Evolution"],
    },
    "B.Sc (Hons) Chemistry": {
        "Sem 1": ["Inorganic Chemistry I", "Physical Chemistry I"],
        "Sem 2": ["Organic Chemistry I", "Physical Chemistry II"],
        "Sem 3": ["Inorganic Chemistry II", "Organic Chemistry II", "Physical Chemistry III"],
        "Sem 4": ["Inorganic Chemistry III", "Organic Chemistry III", "Physical Chemistry IV"],
        "Sem 5": ["Organic Chemistry IV", "Physical Chemistry V"],
        "Sem 6": ["Inorganic Chemistry IV", "Spectroscopy"],
    },
    "B.Sc (prog) Physical science with Chemistry": {
        "Sem 1": ["Atomic Structure & Chemical Bonding", "Mechanics"],
        "Sem 2": ["Organic Chemistry", "Electricity & Magnetism"],
        "Sem 3": ["Thermodynamics", "Waves & Optics"],
        "Sem 4": ["Coordination Chemistry", "Digital Electronics"],
        "Sem 5": ["Quantum Chemistry", "Modern Physics"],
        "Sem 6": ["Spectroscopy", "Solid State Physics"],
    },
    "B.Sc (Hons) Mathematics": {
        "Sem 1": ["Calculus", "Algebra"],
        "Sem 2": ["Real Analysis", "Differential Equations"],
        "Sem 3": ["Theory of Real Functions", "Group Theory I", "Multivariate Calculus"],
        "Sem 4": ["Riemann Integration", "Ring Theory", "Probability & Statistics"],
        "Sem 5": ["Metric Spaces", "Linear Algebra"],
        "Sem 6": ["Complex Analysis", "Numerical Analysis"],
    },
    "B.Sc (Hons) Physics": {
        "Sem 1": ["Mathematical Physics I", "Mechanics"],
        "Sem 2": ["Electricity & Magnetism", "Waves & Optics"],
        "Sem 3": ["Mathematical Physics II", "Thermal Physics", "Digital Systems"],
        "Sem 4": ["Mathematical Physics III", "Elements of Modern Physics", "Analog Systems"],
        "Sem 5": ["Quantum Mechanics", "Solid State Physics"],
        "Sem 6": ["Electromagnetic Theory", "Statistical Mechanics"],
    },
    "B.Sc (Hons) Computer Science": {
        "Sem 1": ["Programming Fundamentals", "Discrete Structures"],
        "Sem 2": ["Object Oriented Programming", "Computer System Architecture"],
        "Sem 3": ["Data Structures", "Operating Systems"],
        "Sem 4": ["Database Management Systems", "Computer Networks"],
        "Sem 5": ["Design & Analysis of Algorithms", "Software Engineering"],
        "Sem 6": ["Artificial Intelligence", "Theory of Computation"],
    },
    "B.Com (Hons)": {
        "Sem 1": ["Financial Accounting", "Business Laws"],
        "Sem 2": ["Corporate Accounting", "Business Mathematics"],
        "Sem 3": ["Cost Accounting", "Business Statistics"],
        "Sem 4": ["Income Tax Law", "Corporate Law"],
        "Sem 5": ["Auditing", "Financial Management"],
        "Sem 6": ["Marketing", "Human Resource Management"],
    },
    "B.A. (Hons) Economics": {
        "Sem 1": ["Introductory Microeconomics", "Mathematical Methods for Economics I"],
        "Sem 2": ["Introductory Macroeconomics", "Mathematical Methods for Economics II"],
        "Sem 3": ["Intermediate Microeconomics I", "Intermediate Macroeconomics I", "Statistical Methods"],
        "Sem 4": ["Intermediate Microeconomics II", "Intermediate Macroeconomics II", "Introductory Econometrics"],
        "Sem 5": ["Indian Economy I", "Development Economics I"],
        "Sem 6": ["Indian Economy II", "Development Economics II"],
    },
}

# Extra words that should also route a file to a subject (optional, used for auto-detect)
SUBJECT_ALIASES = {
    "Biomolecules & Cell Biology": ["cell biology", "biomolecule", "biomolecules"],
    "Phycology & Microbiology": ["phycology", "microbiology", "algae"],
    "Mycology & Phytopathology": ["mycology", "phytopathology"],
    "Archegoniate": ["bryophyte", "pteridophyte", "gymnosperm"],
    "Anatomy of Angiosperms": ["plant anatomy"],
    "Plant Ecology & Phytogeography": ["plant ecology", "phytogeography"],
    "Reproductive Biology of Angiosperms": ["reproductive biology", "embryology"],
    "Animal Physiology: Controlling & Coordinating Systems": ["controlling and coordinating"],
    "Animal Physiology: Life Sustaining Systems": ["life sustaining"],
    "Non-Chordates I": ["non chordates i", "non chordate i", "protista to pseudocoelomates"],
    "Non-Chordates II": ["non chordates ii", "non chordate ii", "coelomate"],
    "Diversity of Chordates": ["chordata", "chordates"],
}

GRADIENTS = [
    ("#D09A68", "#8E5A34"),  # caramel
    ("#8FB07E", "#4F6F4A"),  # fern
    ("#7B9BBA", "#44617F"),  # slate blue
    ("#C98AA1", "#8A4A63"),  # rose
    ("#E0B35E", "#A9741F"),  # amber
    ("#9A8BC4", "#5E4F8C"),  # lavender
    ("#6DB5AD", "#2F7A74"),  # teal
    ("#D77B5F", "#9A3F2A"),  # terracotta
]

# Ordered: first keyword found in the subject name wins
SUBJECT_ICONS = [
    ("non-chord", "fish"), ("chordate", "fish"), ("cell", "microscope"), ("algae", "waves"), ("phyco", "waves"),
    ("myco", "mushroom"), ("pathology", "bug"), ("archegoniate", "sprout"), ("angiosperm", "flower"),
    ("anatomy", "layers"), ("economic botany", "tree"), ("econom", "trending"), ("ecolog", "globe"),
    ("physiolog", "droplet"), ("metabol", "flask"), ("biotech", "dna"), ("systematic", "layers"),
    ("taxonom", "layers"), ("evolution", "paw"), ("development", "sprout"), ("micro", "bug"),
    ("molecul", "dna"), ("genetic", "dna"), ("bio", "dna"), ("inorganic", "flask"), ("organic", "flask"),
    ("chem", "flask"), ("spectro", "waves"), ("thermo", "flame"), ("thermal", "flame"), ("calculus", "infinity"),
    ("algebra", "shapes"), ("analysis", "sigma"), ("group", "shapes"), ("ring", "shapes"), ("mathematical", "sigma"),
    ("mechanic", "atom"), ("optic", "telescope"), ("wave", "waves"), ("electr", "zap"), ("magnet", "magnet"),
    ("digital", "cpu"), ("analog", "cpu"), ("quantum", "atom"), ("solid state", "layers"),
    ("program", "code"), ("python", "code"), ("data", "database"), ("discrete", "network"), ("operating", "monitor"),
    ("network", "network"), ("algorithm", "cpu"), ("artificial", "bulb"), ("account", "wallet"),
    ("tax", "wallet"), ("law", "scale"), ("audit", "file"), ("market", "chart"), ("statist", "pie"),
    ("probab", "dice"), ("econometric", "chart"), ("english", "book-open"), ("politic", "landmark"),
]


def subject_icon(name: str) -> str:
    low = name.lower()
    for kw, emoji in SUBJECT_ICONS:
        if kw in low:
            return emoji
    return "book"


def subject_gradient(name: str):
    return GRADIENTS[zlib.crc32(name.encode()) % len(GRADIENTS)]


def icon_for(course: str, name: str) -> str:
    """Admin-chosen icon if set, otherwise the automatic one."""
    custom = SUBJECT_STYLE.get((course, name), ("", ""))[0]
    return custom or subject_icon(name)


def course_gradient(c: dict):
    col = c.get("color", "")
    if col.isdigit() and int(col) < len(GRADIENTS):
        return GRADIENTS[int(col)]
    return subject_gradient(c["name"])


def gradient_for(course: str, name: str):
    col = SUBJECT_STYLE.get((course, name), ("", ""))[1]
    if col.isdigit() and int(col) < len(GRADIENTS):
        return GRADIENTS[int(col)]
    return subject_gradient(name)


# ───────────────────────── DETECTION LOGIC ─────────────────────────
STOP_WORDS = {"and", "of", "the", "in", "to", "for", "with", "a", "an", "on"}
ROMAN = {"i": "1", "ii": "2", "iii": "3", "iv": "4", "v": "5", "vi": "6"}
CONFUSABLES = str.maketrans({
    "а": "a", "е": "e", "о": "o", "р": "p", "с": "c", "х": "x", "у": "y", "і": "i", "ј": "j", "ѕ": "s", "ο": "o",
})
INVISIBLE = re.compile("[\u200b-\u200f\u2060\ufeff\u00ad]")


def fold(text: str) -> str:
    """Lower-case and flatten fancy fonts (𝑷𝒍𝒂𝒏𝒕, ｐｌａｎｔ, ligatures), accents, odd digits and invisible characters."""
    text = unicodedata.normalize("NFKD", str(text))
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    text = INVISIBLE.sub("", text)
    return text.lower().translate(CONFUSABLES)


def norm(text: str) -> str:
    text = fold(text).replace("&", " and ")
    return " " + re.sub(r"[^a-z0-9+]+", " ", text).strip() + " "


def _stem(w: str) -> str:
    return w[:-1] if len(w) > 3 and w.endswith("s") else w


def tokens(text: str):
    """Words of a name, ignoring symbols, case, plurals, 'and'/'&', and treating I/II/III like 1/2/3."""
    out = []
    for w in norm(text).split():
        if w in STOP_WORDS:
            continue
        out.append(_stem(ROMAN.get(w, w)))
    return out


def _tok_eq(a: str, b: str) -> bool:
    if a == b:
        return True
    if len(a) >= 6 and len(b) >= 6:  # tolerate small typos in long words only
        return difflib.SequenceMatcher(None, a, b).ratio() >= 0.9
    return False


def subject_score(subject_tokens, text_tokens) -> int:
    """All words of the subject must appear in the text (any order). Longer subject names score higher."""
    if not subject_tokens:
        return 0
    for st in subject_tokens:
        if not any(_tok_eq(st, tt) for tt in text_tokens):
            return 0
    return len(subject_tokens)


def kw_in(text: str, keywords) -> bool:
    """Keyword match on word boundaries so 'os' no longer hits 'osmosis' or 'cs' hits 'physics'."""
    for k in keywords:
        body = re.escape(k)
        pat = (r"(?<![a-z0-9])" + body + r"(?![a-z0-9])") if len(k) <= 3 else (r"(?<![a-z0-9])" + body)
        if re.search(pat, text):
            return True
    return False


def detect_subject(course: str, sem: str, text: str) -> str:
    cur = CURRICULUM.get(course)
    if not cur:
        return UNSORTED
    tt = tokens(text)
    if not tt:
        return UNSORTED
    best, best_key = UNSORTED, (0, 0)
    for sm, names in cur.items():
        for name in names:
            score = max(subject_score(tokens(k), tt) for k in [name] + SUBJECT_ALIASES.get(name, []))
            if score:
                key = (score, 1 if sm == sem else 0)  # longest match wins; same semester breaks ties
                if key > best_key:
                    best, best_key = name, key
    return best


def resolve_subject(course: str, sem: str, typed: str, text: str) -> str:
    typed = (typed or "").strip()
    if not typed:
        return detect_subject(course, sem, text)
    for names in CURRICULUM.get(course, {}).values():
        for n in names:
            if n.casefold() == typed.casefold():
                return n
    return typed[:80]


def detect_course(lower: str) -> str:
    if kw_in(lower, ["botany", "plant", "archegoniate", "bryophyte", "pteridophyte", "gymnosperm",
                     "angiosperm", "algae", "microbiology", "mycology", "phytopathology"]):
        return "B.Sc (Hons) Botany"
    if kw_in(lower, ["zoology", "animal", "chordata", "non-chordata", "physiology",
                     "developmental biology", "genetics", "evolution"]):
        return "B.Sc (Hons) Zoology"
    if "life science" in lower or "life-science" in lower:
        return "B.Sc (prog) Life Sciences"
    if "physical science" in lower:
        return "B.Sc (prog) Physical science with Chemistry"
    if kw_in(lower, ["cs", "computer", "c++", "python", "algorithm", "data structure", "dbms", "os"]):
        return "B.Sc (Hons) Computer Science"
    if kw_in(lower, ["math", "calculus", "algebra", "differential", "real analysis"]):
        return "B.Sc (Hons) Mathematics"
    if kw_in(lower, ["chemistry", "organic", "inorganic"]):
        return "B.Sc (Hons) Chemistry"
    if kw_in(lower, ["physic", "mechanics", "optics", "electromagnet", "quantum"]):
        return "B.Sc (Hons) Physics"
    if "bcom hons" in lower or "b.com (h)" in lower:
        return "B.Com (Hons)"
    if "bcom" in lower or "b.com" in lower:
        return "B.Com (Programme)"
    if kw_in(lower, ["econ", "macroeconomics", "microeconomics"]):
        return "B.A. (Hons) Economics"
    if kw_in(lower, ["english", "literature"]):
        return "B.A. (Hons) English"
    if kw_in(lower, ["pol", "constitution", "governance", "political science"]):
        return "B.A. (Hons) Political Science"
    if "ba prog" in lower or "b.a prog" in lower:
        return "B.A. Programme"
    if "cic" in lower or "b.tech" in lower:
        return "B.Tech / CIC"
    return "General / Other"


def pick_course(lower: str, filename: str) -> str:
    """Built-in keyword detection first, then the short titles of any courses the admin added."""
    key = detect_course(lower)
    if key != GENERAL_KEY:
        for c in COURSE_LIST:
            if c["key"] == key:
                return c["name"]
    tt = tokens(filename)
    best, best_score = None, 0
    for c in COURSE_LIST:
        if c["key"] == GENERAL_KEY:
            continue
        sc = subject_score(tokens(c["label"]), tt)
        if sc > best_score:
            best, best_score = c["name"], sc
    if best:
        return best
    for c in COURSE_LIST:
        if c["key"] == GENERAL_KEY:
            return c["name"]
    return GENERAL_KEY


def parse_filename(filename: str, fallback_course: str, fallback_sem: str, fallback_year: str):
    lower = fold(filename)
    detected_year = fallback_year.strip() if fallback_year.strip() else None
    if not detected_year:
        year_match = re.search(r"(?<!\d)(20[1-2][0-9])(?!\d)", lower)
        detected_year = year_match.group(1) if year_match else "2024"
    detected_sem = fallback_sem
    if fallback_sem == "auto":
        sem_match = re.search(r"(?:sem(?:ester)?[\s_-]*([1-6])|\bs([1-6])\b)", lower)
        if sem_match:
            digit = sem_match.group(1) or sem_match.group(2)
            detected_sem = f"Sem {digit}"
        else:
            detected_sem = "Sem 1"
    detected_course = fallback_course
    if fallback_course == "auto":
        detected_course = pick_course(lower, filename)
    base = unicodedata.normalize("NFKC", os.path.splitext(filename)[0])
    clean_title = re.sub(r"[_]+", " ", base).strip().title()
    return clean_title, detected_course, detected_sem, detected_year


# ───────────────────────── DATABASE ─────────────────────────
def reload_curriculum(cursor):
    """Refresh the in-memory subject list (and custom icons/colours) from the du_subjects table."""
    cursor.execute("SELECT course, semester, name, icon, color FROM du_subjects ORDER BY id")
    fresh, style = {}, {}
    for c, sm, n, ic, col in cursor.fetchall():
        fresh.setdefault(c, {}).setdefault(sm, []).append(n)
        if ic or col:
            style[(c, n)] = (ic or "", col or "")
    CURRICULUM.clear()
    CURRICULUM.update(fresh)
    SUBJECT_STYLE.clear()
    SUBJECT_STYLE.update(style)


def reload_courses(cursor):
    cursor.execute("SELECT key, name, label, icon, color FROM du_courses ORDER BY position, id")
    COURSE_LIST[:] = [
        {"key": k, "name": n, "label": l or n, "icon": i or "folder", "color": col or ""}
        for k, n, l, i, col in cursor.fetchall()
    ]


def ensure_subject(cursor, course: str, sem: str, name: str):
    """Register a subject typed during upload/edit if it isn't in the list yet."""
    if not name or name == UNSORTED:
        return
    if any(name in names for names in CURRICULUM.get(course, {}).values()):
        return
    cursor.execute("INSERT OR IGNORE INTO du_subjects (course, semester, name) VALUES (?, ?, ?)", (course, sem, name))
    reload_curriculum(cursor)


def init_db():
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS du_resources (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            title TEXT NOT NULL,
            course TEXT NOT NULL,
            semester TEXT NOT NULL,
            year TEXT,
            type TEXT NOT NULL,
            url_or_name TEXT,
            file_data BLOB,
            file_size TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    conn.commit()
    # Add the new "subject" column to databases created before this update
    try:
        cursor.execute("ALTER TABLE du_resources ADD COLUMN subject TEXT")
        conn.commit()
    except Exception:
        pass  # column already exists
    # Subject folders table (add / rename / move / delete are done from the app)
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS du_subjects (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            course TEXT NOT NULL,
            semester TEXT NOT NULL,
            name TEXT NOT NULL,
            UNIQUE(course, name)
        )
    """)
    conn.commit()
    for col in ("icon", "color"):
        try:
            cursor.execute(f"ALTER TABLE du_subjects ADD COLUMN {col} TEXT")
            conn.commit()
        except Exception:
            pass  # column already exists
    cursor.execute("SELECT COUNT(*) FROM du_subjects")
    if cursor.fetchone()[0] == 0:
        for c, sems in CURRICULUM_SEED.items():
            for sm, names in sems.items():
                for n in names:
                    cursor.execute("INSERT OR IGNORE INTO du_subjects (course, semester, name) VALUES (?, ?, ?)", (c, sm, n))
        conn.commit()
    # Courses table (add / rename / modify / delete from the main screen)
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS du_courses (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            key TEXT NOT NULL UNIQUE,
            name TEXT NOT NULL UNIQUE,
            label TEXT,
            icon TEXT,
            color TEXT,
            position INTEGER DEFAULT 0
        )
    """)
    conn.commit()
    cursor.execute("SELECT COUNT(*) FROM du_courses")
    if cursor.fetchone()[0] == 0:
        for pos, (cname, clabel, cicon) in enumerate(DEFAULT_COURSES):
            cursor.execute(
                "INSERT OR IGNORE INTO du_courses (key, name, label, icon, color, position) VALUES (?, ?, ?, ?, ?, ?)",
                (cname, cname, clabel, cicon, "", pos),
            )
        conn.commit()
    # Courses that only exist on papers (older uploads) get a folder too
    cursor.execute("SELECT DISTINCT course FROM du_resources WHERE course NOT IN (SELECT name FROM du_courses)")
    for (cname,) in cursor.fetchall():
        cursor.execute(
            "INSERT OR IGNORE INTO du_courses (key, name, label, icon, color, position) VALUES (?, ?, ?, ?, ?, ?)",
            (cname, cname, cname[:40], "folder", "", 999),
        )
    conn.commit()
    reload_courses(cursor)
    # Main-screen background image (single row)
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS du_background (
            id INTEGER PRIMARY KEY,
            data BLOB,
            mime TEXT,
            opacity INTEGER DEFAULT 15,
            version INTEGER DEFAULT 1
        )
    """)
    conn.commit()
    # Register subjects that so far exist only on papers (typed during earlier uploads)
    cursor.execute(
        "SELECT course, subject, MIN(semester) FROM du_resources "
        "WHERE subject IS NOT NULL AND subject != '' AND subject != ? GROUP BY course, subject",
        (UNSORTED,),
    )
    for c, subj, sm in cursor.fetchall():
        cursor.execute("INSERT OR IGNORE INTO du_subjects (course, semester, name) VALUES (?, ?, ?)", (c, sm, subj))
    conn.commit()
    reload_curriculum(cursor)
    # One-time sort of old papers into subjects using their titles
    cursor.execute("SELECT id, title, course, semester FROM du_resources WHERE subject IS NULL OR subject = ''")
    rows = cursor.fetchall()
    for rid, title, course, sem in rows:
        cursor.execute("UPDATE du_resources SET subject = ? WHERE id = ?", (detect_subject(course, sem, title), rid))
    if rows:
        conn.commit()
    conn.close()


init_db()
threading.Thread(target=_bg_refresh, daemon=True).start()

# ───────────────────────── PWA ASSETS ─────────────────────────
PWA_MANIFEST = """{
  "name": "DU PYQ Vault",
  "short_name": "DU Vault",
  "id": "/",
  "start_url": "/",
  "scope": "/",
  "display": "standalone",
  "orientation": "any",
  "background_color": "#E8DDD1",
  "theme_color": "#1E1A17",
  "icons": [
    {
      "src": "/icon.svg",
      "sizes": "192x192 512x512",
      "type": "image/svg+xml",
      "purpose": "any"
    },
    {
      "src": "/icon.svg",
      "sizes": "192x192 512x512",
      "type": "image/svg+xml",
      "purpose": "maskable"
    }
  ]
}"""

PWA_ICON_SVG = """<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 512 512" width="512" height="512">
  <rect width="512" height="512" rx="100" fill="#1E1A17"/>
  <circle cx="256" cy="256" r="180" fill="#A77A53"/>
  <text x="256" y="295" font-family="Georgia, serif" font-size="120" font-weight="bold" fill="#FBF8F5" text-anchor="middle">DU</text>
  <text x="256" y="365" font-family="sans-serif" font-size="34" letter-spacing="4" font-weight="bold" fill="#1E1A17" text-anchor="middle">VAULT</text>
</svg>"""

SERVICE_WORKER_JS = """const CACHE_NAME = 'du-vault-cache-v21';
const PRECACHE = [
  '/',
  '/manifest.json',
  '/icon.svg',
  'https://cdnjs.cloudflare.com/ajax/libs/pdf.js/3.11.174/pdf.min.js',
  'https://cdnjs.cloudflare.com/ajax/libs/pdf.js/3.11.174/pdf.worker.min.js'
];
self.addEventListener('install', (e) => {
  // One failed file must not break the whole install
  e.waitUntil(caches.open(CACHE_NAME).then((c) => Promise.all(PRECACHE.map((u) => c.add(u).catch(() => {})))));
  self.skipWaiting();
});
self.addEventListener('activate', (e) => {
  // 'du-vault-keep-*' hold the pages and papers the visitor chose to save; never delete those
  e.waitUntil(
    caches.keys().then((keys) =>
      Promise.all(keys.map((k) => (k !== CACHE_NAME && !k.startsWith('du-vault-keep-') ? caches.delete(k) : null)))
    )
  );
  self.clients.claim();
});
async function handleNavigation(req, url) {
  const cache = await caches.open(CACHE_NAME);
  const network = fetch(req).then((res) => {
    if (res && res.ok) cache.put(req, res.clone());
    return res;
  });
  // Right after an admin action (login, upload, edit...) never show a saved copy
  if (url.searchParams.has('fresh') || url.searchParams.has('notice')) {
    return network.catch(async () => (await caches.match(req)) || (await caches.match('/')) || Response.error());
  }
  const saved = await caches.match(req);
  if (!saved) {
    return network.catch(async () => (await caches.match('/')) || Response.error());
  }
  // Wait up to 3 seconds for the server, then show the saved page instantly
  const timeout = new Promise((resolve) => setTimeout(() => resolve(null), 3000));
  try {
    return (await Promise.race([network, timeout])) || saved;
  } catch (err) {
    return saved;
  }
}
self.addEventListener('fetch', (e) => {
  const req = e.request;
  if (req.method !== 'GET') return;
  const url = new URL(req.url);
  // Let these go straight to the server: PDF downloads/ranges, the catalog, the speed check, and warm-up refreshes
  if (req.headers.has('range') || req.headers.has('x-warm') ||
      url.pathname.startsWith('/view/') || url.pathname.startsWith('/api/') || url.pathname === '/diag') return;
  if (req.mode === 'navigate') {
    e.respondWith(handleNavigation(req, url));
    return;
  }
  // Everything else: serve the saved copy instantly, refresh it in the background
  e.respondWith(
    caches.match(req).then((cachedResponse) => {
      if (cachedResponse) {
        fetch(req).then((networkResponse) => {
          if (networkResponse && networkResponse.status === 200) {
            caches.open(CACHE_NAME).then((cache) => cache.put(req, networkResponse));
          }
        }).catch(() => {});
        return cachedResponse;
      }
      return fetch(req).then((networkResponse) => {
        if (networkResponse && networkResponse.status === 200) {
          const copy = networkResponse.clone();
          caches.open(CACHE_NAME).then((cache) => cache.put(req, copy));
        }
        return networkResponse;
      });
    })
  );
});"""


@app.get("/healthz")
def healthz():
    return {"status": "healthy"}


@app.get("/manifest.json")
def get_manifest():
    return Response(content=PWA_MANIFEST, media_type="application/manifest+json")


@app.get("/sw.js")
def get_sw():
    return Response(content=SERVICE_WORKER_JS, media_type="application/javascript", headers={"Service-Worker-Allowed": "/"})


@app.get("/icon.svg")
def get_icon():
    return Response(content=PWA_ICON_SVG, media_type="image/svg+xml")


# ───────────────────────── HTML TEMPLATE ─────────────────────────
HTML_TEMPLATE = """
<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>DU PYQ Vault</title>
<link rel="manifest" href="/manifest.json">
<meta name="theme-color" content="#1E1A17">
<meta name="apple-mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-status-bar-style" content="black-translucent">
<link rel="icon" type="image/svg+xml" href="/icon.svg">
<link rel="apple-touch-icon" href="/icon.svg">
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&display=swap" rel="stylesheet" media="print" onload="this.media='all'">
<noscript><link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&display=swap" rel="stylesheet"></noscript>
<style>
:root {
    --espresso: #1E1A17;
    --caramel: #A77A53;
    --bg-latte: #E8DDD1;
    --card-foam: #FBF8F5;
    --border-latte: #D8C7B6;
    --text-dark: #261E19;
    --text-muted: #7E6A5B;
}
* { box-sizing: border-box; margin: 0; padding: 0; }
body {
    font-family: 'Inter', sans-serif;
    background-color: var(--bg-latte);
    color: var(--text-dark);
    padding-bottom: 90px;
}
[data-render-badge],
div[class*="render-badge"],
div[id*="render-badge"],
iframe[src*="render.com"],
a[href*="render.com"][style*="fixed"],
a[href*="render.com"][style*="absolute"],
a[href*="render.com"][class*="badge"],
div[style*="z-index"] [style*="fixed"] a[href*="render.com"] {
    display: none !important;
    visibility: hidden !important;
    pointer-events: none !important;
    opacity: 0 !important;
    height: 0 !important;
    width: 0 !important;
    position: absolute !important;
    left: -9999px !important;
    top: -9999px !important;
}
#loading-screen {
    position: fixed;
    top: 0; left: 0; right: 0; bottom: 0;
    background: radial-gradient(circle at center, #2B231D 0%, #151210 100%);
    z-index: 9999;
    display: flex;
    flex-direction: column;
    align-items: center;
    justify-content: center;
    transition: opacity 0.3s ease, visibility 0.3s ease;
}
#loading-screen.fade-out { opacity: 0; visibility: hidden; pointer-events: none; }
.loader-box { position: relative; width: 90px; height: 90px; display: flex; align-items: center; justify-content: center; margin-bottom: 16px; }
.loader-ring {
    position: absolute; width: 100%; height: 100%; border-radius: 50%;
    border: 3px solid rgba(167, 122, 83, 0.2); border-top: 3px solid var(--caramel);
    animation: spinRing 1s cubic-bezier(0.55, 0.055, 0.675, 0.19) infinite;
}
.loader-logo {
    width: 56px; height: 56px; background: var(--caramel); border-radius: 14px;
    display: flex; align-items: center; justify-content: center; color: #FAF6F2;
    font-family: 'Georgia', serif; font-weight: bold; font-size: 1.3rem;
    box-shadow: 0 4px 18px rgba(167, 122, 83, 0.35);
    animation: pulseLogo 1.6s ease-in-out infinite alternate;
}
.loader-text { color: #FAF6F2; font-family: 'Georgia', serif; font-size: 1rem; letter-spacing: 1px; }
.loader-subtext { color: #A77A53; font-size: 0.72rem; letter-spacing: 1.5px; text-transform: uppercase; font-weight: 600; margin-top: 6px; }
@keyframes spinRing { 0% { transform: rotate(0deg); } 100% { transform: rotate(360deg); } }
@keyframes pulseLogo { 0% { transform: scale(0.94); } 100% { transform: scale(1.05); } }
header {
    background: var(--espresso); color: #FAF6F2; padding: 14px 24px;
    position: sticky; top: 0; z-index: 50;
    display: flex; align-items: center; justify-content: space-between;
}
.brand-title { font-family: 'Georgia', serif; font-size: 1.25rem; font-weight: bold; letter-spacing: 0.5px; text-decoration: none; color: #FAF6F2; }
.brand-right { display: flex; align-items: center; gap: 16px; }
.founder-tag { font-family: 'Georgia', serif; font-size: 0.9rem; color: #FAF6F2; text-align: right; line-height: 1.2; }
.founder-tag span.founder-name { color: var(--caramel); font-weight: 700; font-size: 0.95rem; }
.college-subtag { font-size: 0.52rem; color: #BFA898; font-family: 'Inter', sans-serif; text-transform: uppercase; letter-spacing: 0.5px; display: block; }
.admin-lock-btn { background: transparent; border: 1px solid rgba(255,255,255,0.25); color: #FAF6F2; border-radius: 12px; padding: 5px 10px; font-size: 0.75rem; cursor: pointer; text-decoration: none; }
.admin-banner { background: #D4A373; color: #1E1A17; padding: 8px 24px; font-size: 0.82rem; font-weight: 600; display: flex; justify-content: space-between; align-items: center; }
.admin-banner a { color: #641E16; text-decoration: underline; cursor: pointer; }
.container { width: 100%; max-width: 1100px; margin: 0 auto; padding: 20px 16px; }
.folder-grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(180px, 1fr)); gap: 14px; margin: 14px 0 24px 0; }
.folder-card {
    text-decoration: none; color: var(--text-dark); background: var(--card-foam);
    border: 1px solid var(--border-latte); border-radius: 16px; padding: 20px 14px;
    display: flex; flex-direction: column; align-items: center;
    box-shadow: 0 2px 6px rgba(0,0,0,0.04); transition: transform 0.15s ease, box-shadow 0.15s ease;
}
.folder-card:hover { transform: translateY(-2px); box-shadow: 0 6px 14px rgba(0,0,0,0.08); border-color: var(--caramel); }
.folder-card-icon { font-size: 2.4rem; margin-bottom: 8px; }
.folder-card-title { font-size: 0.88rem; font-weight: 700; text-align: center; }
.folder-card-count { font-size: 0.75rem; color: var(--text-muted); margin-top: 4px; }
.folder-header-bar {
    display: flex; align-items: center; justify-content: space-between; gap: 12px;
    background: var(--card-foam); border: 1px solid var(--border-latte);
    border-radius: 16px; padding: 16px 20px; margin-bottom: 16px;
}
.folder-header-title { display: flex; align-items: center; gap: 12px; font-family: 'Georgia', serif; font-size: 1.2rem; font-weight: bold; min-width: 0; }
.back-folder-btn { background: var(--espresso); color: #FAF6F2; text-decoration: none; padding: 8px 16px; border-radius: 12px; font-size: 0.82rem; font-weight: 600; white-space: nowrap; }

/* Breadcrumbs */
.crumbs { display: flex; flex-wrap: wrap; align-items: center; gap: 6px; font-size: 0.8rem; color: var(--text-muted); margin-bottom: 12px; }
.crumbs a { color: var(--caramel); text-decoration: none; font-weight: 600; }
.crumbs a:hover { text-decoration: underline; }

/* Subject folders */
.sem-heading { display: flex; align-items: center; gap: 12px; font-size: 0.95rem; font-weight: 700; color: var(--text-dark); margin: 26px 0 12px; }
.sem-heading::after { content: ""; flex: 1; height: 1px; background: var(--border-latte); }
.subject-grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(150px, 1fr)); gap: 14px; }
.subject-card {
    text-decoration: none; color: var(--text-dark); background: var(--card-foam);
    border: 1px solid var(--border-latte); border-radius: 24px; padding: 20px 12px 16px;
    display: flex; flex-direction: column; align-items: center; text-align: center;
    transition: transform 0.18s ease, box-shadow 0.18s ease, border-color 0.18s ease;
}
.subject-card:hover { transform: translateY(-4px); box-shadow: 0 14px 28px rgba(30,26,23,0.12); border-color: var(--g2); }
.subject-icon {
    position: relative; overflow: hidden; width: 70px; height: 70px; border-radius: 24px;
    display: flex; align-items: center; justify-content: center; font-size: 1.95rem; margin-bottom: 12px;
    background: linear-gradient(145deg, var(--g1), var(--g2));
    box-shadow: 0 10px 18px -8px var(--g2), inset 0 1px 0 rgba(255,255,255,0.4);
}
.subject-icon::after { content: ""; position: absolute; inset: 0; background: linear-gradient(160deg, rgba(255,255,255,0.38) 0%, rgba(255,255,255,0) 55%); }
.subject-icon span { position: relative; z-index: 1; filter: drop-shadow(0 2px 3px rgba(0,0,0,0.25)); }
.subject-title { font-size: 0.84rem; font-weight: 700; line-height: 1.3; }
.subject-count { font-size: 0.72rem; font-weight: 600; color: var(--text-muted); background: var(--bg-latte); padding: 2px 10px; border-radius: 10px; margin-top: 8px; }

.subject-wrap { position: relative; display: flex; }
.subject-wrap .subject-card { flex: 1; }
.subject-admin { position: absolute; top: 8px; right: 8px; display: flex; gap: 4px; }
.sub-btn { width: 26px; height: 26px; border-radius: 50%; border: 1px solid var(--border-latte); background: #fff; color: var(--text-dark); font-size: 0.72rem; cursor: pointer; display: flex; align-items: center; justify-content: center; }
.sub-btn:hover { background: var(--bg-latte); }
.sub-btn.danger { color: #BA1A1A; }
#app-bg { position: fixed; top: 0; left: 0; right: 0; bottom: 0; z-index: 0; background-size: cover; background-position: center; background-repeat: no-repeat; pointer-events: none; }
.container { position: relative; z-index: 1; }
.subject-icon { color: #fff; transition: transform 0.28s cubic-bezier(.34,1.56,.64,1); }
.subject-icon svg { width: 34px; height: 34px; filter: drop-shadow(0 2px 3px rgba(0,0,0,0.28)); }
.subject-card:hover .subject-icon { transform: translateY(-2px) rotate(-5deg) scale(1.08); }
.emoji-opt { color: var(--text-dark); display: flex; align-items: center; justify-content: center; padding: 0; }
.emoji-opt svg { pointer-events: none; }
.emoji-opt.sel { border-color: var(--caramel); background: var(--bg-latte); color: var(--caramel); }
.folder-header-title svg { color: var(--caramel); }
.emoji-grid { display: flex; flex-wrap: wrap; gap: 6px; margin-top: 8px; }
.emoji-opt { width: 34px; height: 34px; border-radius: 10px; border: 1px solid var(--border-latte); background: #fff; font-size: 1.1rem; cursor: pointer; }
.emoji-opt:hover { background: var(--bg-latte); }
.color-grid { display: flex; flex-wrap: wrap; gap: 8px; }
.color-opt { width: 32px; height: 32px; border-radius: 50%; border: 2px solid transparent; cursor: pointer; font-size: 0.6rem; font-weight: 700; color: #fff; }
.color-opt.auto { background: var(--bg-latte); color: var(--text-muted); border: 1px dashed var(--border-latte); }
.color-opt.sel { border-color: var(--espresso); box-shadow: inset 0 0 0 2px #fff; }
.notice { background: #EAF3E3; border: 1px solid #B9D3A8; color: #2F5A1F; padding: 10px 14px; border-radius: 12px; font-size: 0.85rem; font-weight: 600; margin-bottom: 12px; }
.search-box input { width: 100%; padding: 12px 20px; border-radius: 25px; border: 1px solid var(--border-latte); background: var(--card-foam); font-size: 0.95rem; outline: none; margin-bottom: 12px; }
.filters { display: flex; gap: 10px; margin-bottom: 16px; }
select { flex: 1; padding: 11px 14px; border-radius: 18px; border: 1px solid var(--border-latte); background: var(--card-foam); font-size: 0.88rem; outline: none; }
.cards-layout-grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(320px, 1fr)); gap: 14px; }
.card { background: var(--card-foam); border: 1px solid var(--border-latte); border-radius: 18px; padding: 18px; display: flex; flex-direction: column; justify-content: space-between; }
.card-top { display: flex; justify-content: space-between; align-items: center; margin-bottom: 8px; gap: 8px; }
.badge { font-size: 0.78rem; font-weight: 700; color: var(--caramel); }
.admin-actions { display: flex; gap: 8px; align-items: center; }
.edit-btn { background: none; border: none; color: var(--caramel); font-size: 0.85rem; cursor: pointer; font-weight: 600; }
.del-btn { background: none; border: none; color: #BA1A1A; font-size: 1.1rem; cursor: pointer; padding: 0 4px; }
.card-title { font-family: 'Georgia', serif; font-size: 1.05rem; font-weight: bold; margin-bottom: 6px; }
.card-meta { font-size: 0.82rem; color: var(--text-muted); margin-bottom: 14px; }
.btn-pill { display: inline-block; text-align: center; width: 100%; padding: 10px 0; background: var(--espresso); color: #FAF6F2; text-decoration: none; font-size: 0.88rem; font-weight: 600; border-radius: 20px; border: none; cursor: pointer; }
.btn-caramel { background: var(--caramel); }
.fab-bar { position: fixed; bottom: 24px; left: 50%; transform: translateX(-50%); display: flex; gap: 12px; z-index: 100; }
.fab { padding: 13px 22px; border-radius: 30px; background: var(--espresso); color: #FAF6F2; border: none; font-size: 0.92rem; font-weight: 600; box-shadow: 0 4px 16px rgba(0,0,0,0.25); cursor: pointer; }
.modal { display: none; position: fixed; top: 0; left: 0; right: 0; bottom: 0; background: rgba(30,26,23,0.65); z-index: 200; align-items: center; justify-content: center; }
.modal.active { display: flex; }
.modal-content { background: var(--card-foam); width: 92%; max-width: 520px; border-radius: 20px; padding: 26px; max-height: 88vh; overflow-y: auto; }
.modal-title { font-family: 'Georgia', serif; font-size: 1.25rem; font-weight: bold; margin-bottom: 14px; }
.form-group { margin-bottom: 14px; }
.form-group label { display: block; font-size: 0.82rem; font-weight: 600; margin-bottom: 5px; }
.form-group input, .form-group select { width: 100%; padding: 11px; border-radius: 12px; border: 1px solid var(--border-latte); background: var(--bg-latte); outline: none; }
.helper-text { font-size: 0.72rem; color: var(--text-muted); margin-top: 3px; }
#pdfViewerModal { display: none; position: fixed; top: 0; left: 0; right: 0; bottom: 0; background: #14110E; z-index: 99999; flex-direction: column; }
#pdfViewerModal.active { display: flex; }
.pdf-viewer-header { background: var(--espresso); color: #FAF6F2; padding: 12px 20px; display: flex; align-items: center; justify-content: space-between; border-bottom: 1px solid rgba(255,255,255,0.1); gap: 12px; }
.pdf-viewer-title { font-family: 'Georgia', serif; font-size: 0.95rem; font-weight: 600; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; max-width: 55%; }
.pdf-toolbar { display: flex; align-items: center; gap: 10px; }
.zoom-controls { display: flex; background: rgba(255,255,255,0.08); border-radius: 14px; padding: 2px; border: 1px solid rgba(255,255,255,0.15); }
.zoom-btn { background: transparent; color: #FAF6F2; border: none; padding: 6px 12px; border-radius: 10px; font-size: 0.9rem; font-weight: 700; cursor: pointer; transition: background 0.15s ease; }
.zoom-btn:hover { background: rgba(255,255,255,0.15); }
.zoom-btn:active { background: var(--caramel); }
.zoom-level-text { color: #D8C7B6; font-size: 0.78rem; font-weight: 600; padding: 6px 8px; display: flex; align-items: center; min-width: 48px; justify-content: center; }
.pdf-close-btn { background: var(--caramel); color: #FAF6F2; border: none; padding: 7px 15px; border-radius: 12px; font-size: 0.82rem; font-weight: 700; cursor: pointer; }
#pdf-scroll-container { flex: 1; overflow: auto; background: #1E1A17; display: flex; flex-direction: column; align-items: center; padding: 20px 10px 50px 10px; -webkit-overflow-scrolling: touch; user-select: none; -webkit-user-select: none; }
.pdf-canvas-wrap { flex: 0 0 auto; margin: 0 auto 16px auto; box-shadow: 0 4px 20px rgba(0,0,0,0.6); border-radius: 4px; background: #FFFFFF; line-height: 0; }
.pdf-page-canvas { display: block; border-radius: 4px; pointer-events: none; }
.pdf-spinner { width: 44px; height: 44px; border: 3px solid rgba(167, 122, 83, 0.25); border-top: 3px solid var(--caramel); border-radius: 50%; animation: spinRing 0.9s linear infinite; margin-bottom: 14px; }
@media (max-width: 480px) {
    .subject-grid { grid-template-columns: repeat(2, 1fr); }
    .subject-grid { gap: 10px; }
    .subject-card { padding: 12px 8px 12px; border-radius: 20px; }
    .subject-icon { width: 54px; height: 54px; border-radius: 18px; margin-bottom: 8px; }
    .subject-icon svg { width: 27px; height: 27px; }
    .subject-title { font-size: 0.8rem; }
    .cards-layout-grid { grid-template-columns: 1fr; }
}
</style>
</head>
<body>
{bg_layer}
<div id="loading-screen">
    <div class="loader-box">
        <div class="loader-ring"></div>
        <div class="loader-logo">DU</div>
    </div>
    <div class="loader-text">DU PYQ Vault</div>
    <div class="loader-subtext">Opening Vault...</div>
</div>
<header>
    <a href="/" class="brand-title">DU VAULT</a>
    <div class="brand-right">
        <div class="founder-tag">
            <div> C/B- <span class="founder-name">Shrinjay</span></div>
            <span class="college-subtag">(Hansraj College)</span>
        </div>
        {admin_header_btn}
    </div>
</header>
{admin_banner_html}
<div class="container">
    {main_view_content}
</div>
{fab_controls}
{bg_modal}
<datalist id="subjectList">{subject_datalist}</datalist>
<div id="pdfViewerModal">
    <div class="pdf-viewer-header">
        <div class="pdf-viewer-title" id="pdfModalTitle">Viewing Paper</div>
        <div class="pdf-toolbar">
            <div class="zoom-controls">
                <button class="zoom-btn" onclick="adjustZoom(-0.25)" title="Zoom Out">-</button>
                <div class="zoom-level-text" id="zoomLevelDisplay">100%</div>
                <button class="zoom-btn" onclick="adjustZoom(0.25)" title="Zoom In">+</button>
                <button class="zoom-btn" onclick="resetZoom()" title="Fit to Screen" style="border-left: 1px solid rgba(255,255,255,0.15);">⟲</button>
            </div>
            <button class="pdf-close-btn" onclick="closePdfViewer()">✕ Back to List</button>
        </div>
    </div>
    <div id="pdf-scroll-container" oncontextmenu="return false;"></div>
</div>
<div class="modal" id="uploadModal" onclick="if(event.target === this) closeModal('uploadModal')">
    <div class="modal-content">
        <div class="modal-title">Batch Upload PDFs</div>
        <form action="/upload" method="POST" enctype="multipart/form-data" onsubmit="showLoader()">
            <div class="form-group">
                <label>Course Categorization</label>
                <select name="course">
                    <option value="auto">⚡ Auto-Detect from Filename</option>
                    {upload_course_options}
                </select>
                <div class="helper-text">Botany, Zoology, Physics, CS papers auto-sort into their respective folders.</div>
            </div>
            <div class="form-group">
                <label>Semester</label>
                <select name="sem">
                    <option value="auto">⚡ Auto-Detect from Filename</option>
                    {upload_sem_options}
                </select>
                <div class="helper-text">Detects "Sem 1", "Sem 3", "Semester 6", etc. in names.</div>
            </div>
            <div class="form-group">
                <label>Subject</label>
                <input type="text" name="subject" list="subjectList" placeholder="Leave blank to auto-detect" autocomplete="off">
                <div class="helper-text">Pick from the list or type a new subject. Blank = detected from the filename, else "Unsorted".</div>
            </div>
            <div class="form-group">
                <label>Exam Year</label>
                <input type="text" name="year" placeholder="e.g. 2024 or leave blank for Auto">
                <div class="helper-text">Detects 4-digit years (e.g. 2022, 2023) if left blank.</div>
            </div>
            <div class="form-group">
                <label>Select All PDF Files</label>
                <input type="file" name="files" accept="application/pdf" multiple required>
            </div>
            <button type="submit" class="btn-pill" style="margin-top: 10px;">Upload Entire Batch</button>
        </form>
    </div>
</div>
<div class="modal" id="linkModal" onclick="if(event.target === this) closeModal('linkModal')">
    <div class="modal-content">
        <div class="modal-title">Add Reference Link</div>
        <form action="/add-link" method="POST">
            <div class="form-group"><label>Title</label><input type="text" name="title" placeholder="e.g. Official Syllabus" required></div>
            <div class="form-group"><label>URL</label><input type="url" name="url" placeholder="https://..." required></div>
            <div class="form-group"><label>Course</label><select name="course" required>{upload_course_options}</select></div>
            <div class="form-group"><label>Semester</label><select name="sem" required>{upload_sem_options}</select></div>
            <div class="form-group"><label>Subject</label><input type="text" name="subject" list="subjectList" placeholder="Optional" autocomplete="off"></div>
            <button type="submit" class="btn-pill btn-caramel" style="margin-top: 8px;">Save Link</button>
        </form>
    </div>
</div>
<div class="modal" id="editModal" onclick="if(event.target === this) closeModal('editModal')">
    <div class="modal-content">
        <div class="modal-title">Edit Resource Details</div>
        <form action="/edit-item" method="POST" onsubmit="showLoader()">
            <input type="hidden" name="item_id" id="edit_item_id">
            <div class="form-group"><label>Title</label><input type="text" name="title" id="edit_title" required></div>
            <div class="form-group"><label>Course</label><select name="course" id="edit_course">{upload_course_options}</select></div>
            <div class="form-group"><label>Semester</label><select name="sem" id="edit_sem">{upload_sem_options}</select></div>
            <div class="form-group"><label>Subject</label><input type="text" name="subject" id="edit_subject" list="subjectList" placeholder="e.g. Cell Biology" autocomplete="off"></div>
            <div class="form-group"><label>Exam Year</label><input type="text" name="year" id="edit_year" required></div>
            <button type="submit" class="btn-pill" style="margin-top: 8px;">Save Changes</button>
        </form>
    </div>
</div>
<div class="modal" id="subjectAddModal" onclick="if(event.target === this) closeModal('subjectAddModal')">
    <div class="modal-content">
        <div class="modal-title">Add Subject</div>
        <form action="/subject/add" method="POST" onsubmit="showLoader()">
            <input type="hidden" name="course" id="sa_course">
            <div class="form-group"><label>Subject name</label><input type="text" name="name" maxlength="80" placeholder="e.g. Cell Biology" required></div>
            <div class="form-group"><label>Semester</label><select name="sem">{upload_sem_options}</select></div>
            <div class="form-group"><label>Icon <span style="font-weight:400;color:var(--text-muted);">(optional: pick one or type any emoji)</span></label>
                <input type="text" name="icon" id="sa_icon" maxlength="16" placeholder="Auto" autocomplete="off">
                <div class="emoji-grid" data-target="sa_icon"></div></div>
            <div class="form-group"><label>Colour</label><input type="hidden" name="color" id="sa_color" value=""><div class="color-grid" data-target="sa_color"></div></div>
            <div class="form-group"><label style="display:flex; gap:8px; align-items:flex-start; font-weight:500;">
                <input type="checkbox" name="pull_from_others" value="1" style="width:auto; margin-top:3px;">
                <span>Also pull matching papers from other subjects in this course</span></label>
                <div class="helper-text">Papers in "Unsorted" whose names match are always moved in automatically.</div></div>
            <button type="submit" class="btn-pill btn-caramel" style="margin-top: 8px;">Add Subject</button>
        </form>
    </div>
</div>
<div class="modal" id="subjectEditModal" onclick="if(event.target === this) closeModal('subjectEditModal')">
    <div class="modal-content">
        <div class="modal-title">Rename / Move Subject</div>
        <form action="/subject/edit" method="POST" onsubmit="showLoader()">
            <input type="hidden" name="course" id="se_course">
            <input type="hidden" name="old_name" id="se_old">
            <div class="form-group"><label>Subject name</label><input type="text" name="new_name" id="se_name" maxlength="80" required></div>
            <div class="form-group"><label>Semester</label><select name="sem" id="se_sem">{upload_sem_options}</select></div>
            <div class="form-group"><label>Icon <span style="font-weight:400;color:var(--text-muted);">(empty = automatic)</span></label>
                <input type="text" name="icon" id="se_icon" maxlength="16" placeholder="Auto" autocomplete="off">
                <div class="emoji-grid" data-target="se_icon"></div></div>
            <div class="form-group"><label>Colour</label><input type="hidden" name="color" id="se_color" value=""><div class="color-grid" data-target="se_color"></div></div>
            <div class="helper-text">Renaming also updates every paper filed under this subject.</div>
            <button type="submit" class="btn-pill" style="margin-top: 12px;">Save Changes</button>
        </form>
    </div>
</div>
<div class="modal" id="offlineModal" onclick="if(event.target === this) { offlineAbort = true; closeModal('offlineModal'); }">
    <div class="modal-content">
        <div class="modal-title">Save for offline &amp; faster use</div>
        <div id="offStatus" style="font-size:0.85rem; color:var(--text-muted); margin-bottom:12px;">Checking...</div>
        <div style="height:10px; background:var(--bg-latte); border-radius:6px; overflow:hidden; margin-bottom:14px;">
            <div id="offBar" style="height:100%; width:0; background:var(--caramel); transition:width 0.2s;"></div>
        </div>
        <button type="button" class="btn-pill" onclick="downloadOffline(false)">Download pages only (small)</button>
        <button type="button" class="btn-pill btn-caramel" id="offAll" style="margin-top:10px;" onclick="downloadOffline(true)">Download pages + all papers</button>
        <button type="button" class="btn-pill" style="margin-top:10px; background:#BA1A1A;" onclick="clearOffline()">Remove saved data</button>
        <div class="helper-text" style="margin-top:10px;">Saved on this device only. Saved papers open instantly and work without internet. Run it again any time to fetch new papers.</div>
    </div>
</div>
<div class="modal" id="courseAddModal" onclick="if(event.target === this) closeModal('courseAddModal')">
    <div class="modal-content">
        <div class="modal-title">Add Course</div>
        <form action="/course/add" method="POST" onsubmit="showLoader()">
            <div class="form-group"><label>Course name</label><input type="text" name="name" maxlength="80" placeholder="e.g. B.Sc (Hons) Statistics" required></div>
            <div class="form-group"><label>Short title on the tile</label><input type="text" name="label" maxlength="40" placeholder="e.g. Statistics"></div>
            <div class="form-group"><label>Icon <span style="font-weight:400;color:var(--text-muted);">(pick one or type any emoji)</span></label>
                <input type="text" name="icon" id="ca_icon" maxlength="16" placeholder="Auto" autocomplete="off">
                <div class="emoji-grid" data-target="ca_icon"></div></div>
            <div class="form-group"><label>Colour</label><input type="hidden" name="color" id="ca_color" value=""><div class="color-grid" data-target="ca_color"></div></div>
            <button type="submit" class="btn-pill btn-caramel" style="margin-top: 8px;">Add Course</button>
        </form>
    </div>
</div>
<div class="modal" id="courseEditModal" onclick="if(event.target === this) closeModal('courseEditModal')">
    <div class="modal-content">
        <div class="modal-title">Edit Course</div>
        <form action="/course/edit" method="POST" onsubmit="showLoader()">
            <input type="hidden" name="old_name" id="ce_old">
            <div class="form-group"><label>Course name</label><input type="text" name="name" id="ce_name" maxlength="80" required></div>
            <div class="form-group"><label>Short title on the tile</label><input type="text" name="label" id="ce_label" maxlength="40"></div>
            <div class="form-group"><label>Icon</label>
                <input type="text" name="icon" id="ce_icon" maxlength="16" placeholder="Auto" autocomplete="off">
                <div class="emoji-grid" data-target="ce_icon"></div></div>
            <div class="form-group"><label>Colour</label><input type="hidden" name="color" id="ce_color" value=""><div class="color-grid" data-target="ce_color"></div></div>
            <div class="helper-text">Renaming also updates every subject and paper filed under this course.</div>
            <button type="submit" class="btn-pill" style="margin-top: 12px;">Save Changes</button>
        </form>
    </div>
</div>
<div class="modal" id="loginModal" onclick="if(event.target === this) closeModal('loginModal')">
    <div class="modal-content">
        <div class="modal-title">Host Admin Access</div>
        <form action="/login" method="POST">
            <div class="form-group">
                <label>Enter Admin Password</label>
                <input type="password" name="password" placeholder="Password" required autofocus>
            </div>
            <button type="submit" class="btn-pill" style="margin-top: 8px;">Authenticate</button>
        </form>
    </div>
</div>
<script>
    // The PDF viewer library is loaded only when needed (and quietly in the background on paper lists)
    let pdfJsPromise = null;
    function loadPdfJs() {
        if (window.pdfjsLib) return Promise.resolve();
        if (!pdfJsPromise) {
            pdfJsPromise = new Promise((resolve, reject) => {
                const sc = document.createElement('script');
                sc.src = 'https://cdnjs.cloudflare.com/ajax/libs/pdf.js/3.11.174/pdf.min.js';
                sc.onload = () => {
                    pdfjsLib.GlobalWorkerOptions.workerSrc = 'https://cdnjs.cloudflare.com/ajax/libs/pdf.js/3.11.174/pdf.worker.min.js';
                    resolve();
                };
                sc.onerror = () => { pdfJsPromise = null; reject(new Error('pdf.js failed to load')); };
                document.head.appendChild(sc);
            });
        }
        return pdfJsPromise;
    }
    window.addEventListener('load', () => {
        if (document.querySelector('.card-item')) { setTimeout(() => loadPdfJs().catch(() => {}), 1500); }
    });
    function openModal(id) { document.getElementById(id).classList.add('active'); }
    function closeModal(id) { document.getElementById(id).classList.remove('active'); }
    function showLoader() {
        const loader = document.getElementById('loading-screen');
        if (loader) loader.classList.remove('fade-out');
    }
    function hideLoader() {
        const loader = document.getElementById('loading-screen');
        if (loader) loader.classList.add('fade-out');
    }
    // ── Offline pack & background warm-up ──
    const KEEP_PAGES = 'du-vault-keep-pages';
    const KEEP_PDFS = 'du-vault-keep-pdfs';
    let offlineAbort = false;
    let catalogData = null;
    const fmtMB = (b) => (b / 1048576).toFixed(b > 10485760 ? 0 : 1) + ' MB';
    async function loadCatalog() {
        if (!catalogData) { catalogData = await (await fetch('/api/catalog')).json(); }
        return catalogData;
    }
    function setOff(text, pct) {
        document.getElementById('offStatus').textContent = text;
        document.getElementById('offBar').style.width = Math.max(0, Math.min(100, pct)) + '%';
    }
    async function openOffline() {
        openModal('offlineModal');
        setOff('Checking what is available...', 0);
        try {
            const cat = await loadCatalog();
            const have = (await (await caches.open(KEEP_PDFS)).keys()).length;
            let space = '';
            if (navigator.storage && navigator.storage.estimate) {
                const e = await navigator.storage.estimate();
                space = ' Free space on this device: about ' + fmtMB(Math.max(0, e.quota - e.usage)) + '.';
            }
            document.getElementById('offAll').textContent = 'Download pages + all papers (' + fmtMB(cat.total_bytes) + ')';
            setOff(cat.papers.length + ' papers available (' + fmtMB(cat.total_bytes) + '). ' + have + ' already saved on this device.' + space, 0);
        } catch (err) {
            setOff('Could not reach the server. Please try again in a moment.', 0);
        }
    }
    async function savePages(cat, onStep) {
        const cache = await caches.open(KEEP_PAGES);
        for (const url of cat.pages) {
            if (offlineAbort) return;
            try {
                const r = await fetch(url, { credentials: 'same-origin', headers: { 'X-Warm': '1' } });
                if (r.ok) await cache.put(url, r);
            } catch (err) { if (err && err.name === 'QuotaExceededError') throw err; }
            onStep();
        }
    }
    async function downloadOffline(withPdfs) {
        offlineAbort = false;
        try {
            const cat = await loadCatalog();
            if (navigator.storage && navigator.storage.persist) { navigator.storage.persist(); }
            const total = cat.pages.length + (withPdfs ? cat.papers.length : 0);
            let done = 0, bytes = 0;
            await savePages(cat, () => { done++; setOff('Saving pages... ' + done + '/' + total, (done / total) * 100); });
            if (withPdfs) {
                const pdfs = await caches.open(KEEP_PDFS);
                const queue = cat.papers.slice();
                const worker = async () => {
                    while (queue.length && !offlineAbort) {
                        const p = queue.shift();
                        const key = '/view/' + p.id;
                        try {
                            if (!(await pdfs.match(key))) {
                                const r = await fetch(key);
                                if (r.ok) { await pdfs.put(key, r); bytes += p.bytes; }
                            }
                        } catch (err) { if (err && err.name === 'QuotaExceededError') { offlineAbort = true; throw err; } }
                        done++;
                        setOff('Saving papers... ' + done + '/' + total + ' (' + fmtMB(bytes) + ')', (done / total) * 100);
                    }
                };
                await Promise.all([worker(), worker(), worker()]);
            }
            setOff(offlineAbort ? 'Stopped.' : 'Done! Everything saved now loads instantly on this device.', offlineAbort ? 0 : 100);
        } catch (err) {
            setOff(err && err.name === 'QuotaExceededError' ? 'This device ran out of storage. Free some space and try again.' : 'Something went wrong. Please try again.', 0);
        }
    }
    async function clearOffline() {
        await caches.delete(KEEP_PAGES);
        await caches.delete(KEEP_PDFS);
        localStorage.removeItem('du_warm');
        setOff('Saved data removed from this device.', 0);
    }
    // Quietly refresh the saved pages (not the papers) at most once a day, only on a decent connection
    async function warmPages() {
        try {
            const c = navigator.connection;
            if (c && (c.saveData || /(^|-)2g$/.test(c.effectiveType || ''))) return;
            if (Date.now() - Number(localStorage.getItem('du_warm') || 0) < 24 * 3600 * 1000) return;
            const cat = await loadCatalog();
            const cache = await caches.open(KEEP_PAGES);
            for (const url of cat.pages) {
                const r = await fetch(url, { credentials: 'same-origin', headers: { 'X-Warm': '1' } });
                if (r.ok) await cache.put(url, r);
                await new Promise((res) => setTimeout(res, 200));
            }
            localStorage.setItem('du_warm', String(Date.now()));
        } catch (err) {}
    }
    window.addEventListener('load', () => { setTimeout(warmPages, 5000); });
    document.addEventListener('DOMContentLoaded', () => { setTimeout(hideLoader, 60); });
    window.addEventListener('load', hideLoader);

    // Fix bfcache restoration lag when using right swipe or browser back button
    window.addEventListener('pageshow', (event) => {
        if (event.persisted) { hideLoader(); }
    });

    document.addEventListener('DOMContentLoaded', () => {
        const triggers = document.querySelectorAll('a.folder-card, a.back-folder-btn, a.subject-card, .crumbs a');
        triggers.forEach(el => {
            el.addEventListener('click', (e) => {
                if (!e.ctrlKey && !e.metaKey && !el.target) { showLoader(); }
            });
        });
    });
    let activePdfDoc = null;
    let currentZoomMultiplier = 1.0;
    async function openPdfViewer(url, title) {
        document.getElementById('pdfModalTitle').innerText = title;
        currentZoomMultiplier = 1.0;
        updateZoomDisplay();
        const container = document.getElementById('pdf-scroll-container');
        container.innerHTML = `
            <div style="display: flex; flex-direction: column; align-items: center; justify-content: center; margin-top: 80px; color: #FAF6F2;">
                <div class="pdf-spinner"></div>
                <div style="font-size: 0.88rem; letter-spacing: 0.5px;">Loading Document...</div>
            </div>
        `;
        document.getElementById('pdfViewerModal').classList.add('active');
        history.pushState({ pdfOpen: true }, '');
        try {
            await loadPdfJs();
            let source = { url: url, disableAutoFetch: true, rangeChunkSize: 131072 };
            try {
                const saved = await caches.match(url);  // saved on this device? then open instantly, no download
                if (saved) { source = { data: new Uint8Array(await saved.arrayBuffer()) }; }
            } catch (err) {}
            const loadingTask = pdfjsLib.getDocument(source);
            activePdfDoc = await loadingTask.promise;
            await renderPdfPages();
        } catch (err) {
            console.error(err);
            container.innerHTML = `
                <div style="color: #FAF6F2; padding: 40px 20px; text-align: center;">
                    <p>Failed to load document preview.</p>
                </div>
            `;
        }
    }
    let pdfObserver = null;
    let renderToken = 0;
    async function renderPdfPages() {
        if (!activePdfDoc) return;
        const token = ++renderToken;
        if (pdfObserver) { pdfObserver.disconnect(); pdfObserver = null; }
        const container = document.getElementById('pdf-scroll-container');
        container.innerHTML = '';
        const dpr = Math.min(window.devicePixelRatio || 1, 2);
        const availableWidth = Math.min(window.innerWidth - 32, 950);
        const cssScaleFor = (page) => (availableWidth / page.getViewport({ scale: 1.0 }).width) * currentZoomMultiplier;
        // Size placeholders from page 1 so the scrollbar is right immediately; real pages are drawn only when they come into view
        const first = await activePdfDoc.getPage(1);
        if (token !== renderToken) return;
        const v1 = first.getViewport({ scale: cssScaleFor(first) });
        async function drawPage(wrap) {
            if (token !== renderToken || !activePdfDoc) return;
            const page = await activePdfDoc.getPage(Number(wrap.dataset.page));
            if (token !== renderToken) return;
            const scale = cssScaleFor(page);
            const cssViewport = page.getViewport({ scale: scale });
            const highRes = page.getViewport({ scale: scale * dpr });
            const canvas = document.createElement('canvas');
            canvas.className = 'pdf-page-canvas';
            canvas.width = Math.floor(highRes.width);
            canvas.height = Math.floor(highRes.height);
            canvas.style.width = Math.floor(cssViewport.width) + 'px';
            canvas.style.height = Math.floor(cssViewport.height) + 'px';
            wrap.style.width = canvas.style.width;
            wrap.style.height = canvas.style.height;
            wrap.appendChild(canvas);
            await page.render({ canvasContext: canvas.getContext('2d'), viewport: highRes }).promise;
        }
        pdfObserver = new IntersectionObserver((entries) => {
            entries.forEach((en) => {
                if (en.isIntersecting) { pdfObserver.unobserve(en.target); drawPage(en.target).catch(() => {}); }
            });
        }, { root: container, rootMargin: '700px 0px' });
        for (let n = 1; n <= activePdfDoc.numPages; n++) {
            const wrap = document.createElement('div');
            wrap.className = 'pdf-canvas-wrap';
            wrap.dataset.page = n;
            wrap.style.width = Math.floor(v1.width) + 'px';
            wrap.style.height = Math.floor(v1.height) + 'px';
            container.appendChild(wrap);
            pdfObserver.observe(wrap);
        }
    }
    function updateZoomDisplay() {
        const display = document.getElementById('zoomLevelDisplay');
        if (display) { display.innerText = Math.round(currentZoomMultiplier * 100) + '%'; }
    }
    function adjustZoom(delta) {
        const nextZoom = currentZoomMultiplier + delta;
        if (nextZoom >= 0.5 && nextZoom <= 3.0) {
            currentZoomMultiplier = Math.round(nextZoom * 100)/100;
            updateZoomDisplay();
            renderPdfPages();
        }
    }
    function resetZoom() {
        currentZoomMultiplier = 1.0;
        updateZoomDisplay();
        renderPdfPages();
    }
    function closePdfViewer(isPopState = false) {
        const viewer = document.getElementById('pdfViewerModal');
        if (viewer.classList.contains('active')) {
            viewer.classList.remove('active');
            document.getElementById('pdf-scroll-container').innerHTML = '';
            activePdfDoc = null;
            renderToken++;
            if (pdfObserver) { pdfObserver.disconnect(); pdfObserver = null; }
            if (!isPopState && history.state && history.state.pdfOpen) { history.back(); }
        }
    }
    window.addEventListener('popstate', (e) => {
        const viewer = document.getElementById('pdfViewerModal');
        if (viewer && viewer.classList.contains('active')) { closePdfViewer(true); }
    });
    function openEditModal(btn) {
        const d = btn.dataset;
        document.getElementById('edit_item_id').value = d.id;
        document.getElementById('edit_title').value = d.title;
        document.getElementById('edit_course').value = d.course;
        document.getElementById('edit_sem').value = d.sem;
        document.getElementById('edit_subject').value = d.subject;
        document.getElementById('edit_year').value = d.year;
        openModal('editModal');
    }
    const GRADS = {gradients_json};
    const ICONS = {icons_json};
    function svgIcon(key, size) {
        return '<svg viewBox="0 0 24 24" width="' + size + '" height="' + size + '" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round">' + ICONS[key] + '</svg>';
    }
    function markColors(inputId) {
        const val = document.getElementById(inputId).value;
        const grid = document.querySelector('.color-grid[data-target="' + inputId + '"]');
        grid.querySelectorAll('.color-opt').forEach(b => b.classList.toggle('sel', b.dataset.v === val));
    }
    function markIcons(inputId) {
        const val = document.getElementById(inputId).value;
        const grid = document.querySelector('.emoji-grid[data-target="' + inputId + '"]');
        grid.querySelectorAll('.emoji-opt').forEach(b => b.classList.toggle('sel', b.dataset.v === val));
    }
    function buildPickers() {
        document.querySelectorAll('.emoji-grid').forEach(g => {
            const input = document.getElementById(g.dataset.target);
            Object.keys(ICONS).forEach(k => {
                const b = document.createElement('button');
                b.type = 'button'; b.className = 'emoji-opt'; b.title = k; b.dataset.v = k;
                b.innerHTML = svgIcon(k, 18);
                b.onclick = () => { input.value = k; markIcons(g.dataset.target); };
                g.appendChild(b);
            });
            input.addEventListener('input', () => markIcons(g.dataset.target));
        });
        document.querySelectorAll('.color-grid').forEach(g => {
            const input = document.getElementById(g.dataset.target);
            const make = (label, v, bg) => {
                const b = document.createElement('button');
                b.type = 'button'; b.className = 'color-opt' + (v === '' ? ' auto' : ''); b.dataset.v = v;
                if (bg) b.style.background = bg;
                if (label) b.textContent = label;
                b.onclick = () => { input.value = v; markColors(g.dataset.target); };
                g.appendChild(b);
            };
            make('Auto', '', null);
            GRADS.forEach((p, i) => make('', String(i), 'linear-gradient(145deg,' + p[0] + ',' + p[1] + ')'));
        });
    }
    document.addEventListener('DOMContentLoaded', buildPickers);
    function setPicker(prefix, icon, color) {
        document.getElementById(prefix + '_icon').value = icon || '';
        document.getElementById(prefix + '_color').value = color || '';
        markIcons(prefix + '_icon');
        markColors(prefix + '_color');
    }
    function openAddSubject(btn) {
        document.getElementById('sa_course').value = btn.dataset.course;
        setPicker('sa', '', '');
        openModal('subjectAddModal');
    }
    function openSubjectModal(btn) {
        const d = btn.dataset;
        document.getElementById('se_course').value = d.course;
        document.getElementById('se_old').value = d.name;
        document.getElementById('se_name').value = d.name;
        document.getElementById('se_sem').value = d.sem;
        setPicker('se', d.icon, d.color);
        openModal('subjectEditModal');
    }
    function openAddCourse() {
        setPicker('ca', '', '');
        openModal('courseAddModal');
    }
    function openCourseModal(btn) {
        const d = btn.dataset;
        document.getElementById('ce_old').value = d.name;
        document.getElementById('ce_name').value = d.name;
        document.getElementById('ce_label').value = d.label;
        setPicker('ce', d.icon, d.color);
        openModal('courseEditModal');
    }
    function filterCardsLive(query) {
        const term = query.toLowerCase().trim();
        document.querySelectorAll('.card-item').forEach(card => {
            const text = card.getAttribute('data-search-text') || '';
            card.style.display = text.includes(term) ? 'flex' : 'none';
        });
    }
    if ('serviceWorker' in navigator) {
        window.addEventListener('load', () => {
            navigator.serviceWorker.register('/sw.js', { scope: '/' });
        });
    }
</script>
</body>
</html>
"""


# ───────────────────────── VIEW BUILDERS ─────────────────────────
def subject_card_html(course: str, name: str, count: int, admin: bool = False, sem: str = "") -> str:
    card = _subject_card_core(course, name, count)
    if not admin:
        return card
    return f"""
    <div class="subject-wrap">
        {card}
        <div class="subject-admin">
            <button type="button" class="sub-btn" title="Rename / move to another semester"
                data-course="{esc(course)}" data-name="{esc(name)}" data-sem="{esc(sem)}"
                data-icon="{esc(SUBJECT_STYLE.get((course, name), ("", ""))[0])}"
                data-color="{esc(SUBJECT_STYLE.get((course, name), ("", ""))[1])}"
                onclick="openSubjectModal(this)">✎</button>
            <form action="/subject/delete" method="POST"
                onsubmit="return confirm('Delete this subject folder? Its papers are kept and moved to Unsorted.');">
                <input type="hidden" name="course" value="{esc(course)}">
                <input type="hidden" name="name" value="{esc(name)}">
                <button type="submit" class="sub-btn danger" title="Delete subject">✕</button>
            </form>
        </div>
    </div>
    """


def _subject_card_core(course: str, name: str, count: int) -> str:
    g1, g2 = gradient_for(course, name)
    href = f"/?course={urlq(course)}&subject={urlq(name)}"
    label = "paper" if count == 1 else "papers"
    return f"""
    <a href="{esc(href)}" class="subject-card" style="--g1:{g1};--g2:{g2}">
        <div class="subject-icon"><span>{icon_svg(icon_for(course, name))}</span></div>
        <div class="subject-title">{esc(name)}</div>
        <div class="subject-count">{count} {label}</div>
    </a>
    """


def course_card_html(c: dict, count: int, admin: bool) -> str:
    g1, g2 = course_gradient(c)
    href = f"/?course={urlq(c['name'])}"
    label = "paper" if count == 1 else "papers"
    card = f"""
    <a href="{esc(href)}" class="subject-card" style="--g1:{g1};--g2:{g2}">
        <div class="subject-icon"><span>{icon_svg(c["icon"])}</span></div>
        <div class="subject-title">{esc(c["label"])}</div>
        <div class="subject-count">{count} {label}</div>
    </a>
    """
    if not admin:
        return card
    return f"""
    <div class="subject-wrap">
        {card}
        <div class="subject-admin">
            <button type="button" class="sub-btn" title="Rename / change icon"
                data-name="{esc(c["name"])}" data-label="{esc(c["label"])}"
                data-icon="{esc(c["icon"])}" data-color="{esc(c["color"])}"
                onclick="openCourseModal(this)">✎</button>
            <form action="/course/delete" method="POST"
                onsubmit="return confirm('Delete this course? Its subject folders are removed and its papers move to General / Other.');">
                <input type="hidden" name="name" value="{esc(c["name"])}">
                <button type="submit" class="sub-btn danger" title="Delete course">✕</button>
            </form>
        </div>
    </div>
    """


def crumbs_html(course: str, subject: str, q: str) -> str:
    parts = ['<a href="/">Home</a>']
    if course:
        tile = course_tile(course)
        label = tile["label"] if tile else course
        if subject or q:
            parts.append(f'<a href="/?course={esc(urlq(course))}">{esc(label)}</a>')
        else:
            parts.append(f"<span>{esc(label)}</span>")
    if subject:
        parts.append(f"<span>{esc(subject)}</span>")
    elif q:
        parts.append(f"<span>Search: {esc(q)}</span>")
    return '<nav class="crumbs">' + " <span>›</span> ".join(parts) + "</nav>"


@app.get("/", response_class=HTMLResponse)
def index(request: Request, q: str = "", course: str = "", sem: str = "All Semesters", subject: str = "", notice: str = "", year: str = ""):
    admin = is_admin(request)
    ensure_snapshot()  # keeps the in-memory catalogue fresh (quietly, in the background)
    if admin or notice:
        return HTMLResponse(content=render_index(admin, q, course, sem, subject, notice, year))
    # Visitors all get the same page, so it is built once and reused (cleared whenever the admin changes anything)
    page = cached(("html", q, course, sem, subject, year), lambda: render_index(False, q, course, sem, subject, "", year))
    return HTMLResponse(content=page)


def render_index(admin: bool, q: str, course: str, sem: str, subject: str, notice: str, year: str) -> str:
    q = q.strip()
    ensure_snapshot()
    main_view_content = ""
    bg_layer = ""
    bg_modal = ""

    # ── LEVEL 1: Home → course folders
    if not course and not q and not subject:
        counts = snap_counts()
        folders_grid = '<div style="font-size: 0.85rem; font-weight: 700; text-transform: uppercase; color: var(--text-muted); margin: 6px 0 12px;">Select Course Folder</div>'
        if COURSE_LIST:
            folders_grid += '<div class="subject-grid">' + "".join(
                course_card_html(c, counts.get(c["name"], 0), admin) for c in COURSE_LIST
            ) + "</div>"
        else:
            folders_grid += '<div class="card" style="text-align:center; padding:40px;"><p style="color:var(--text-muted);">No courses yet.</p></div>'
        search_bar = """
        <form method="GET" action="/">
            <div class="search-box" style="margin-top: 10px;">
                <input type="text" name="q" placeholder="🔍 Search any paper across all courses...">
            </div>
        </form>
        """
        tool_btns = (
            '<button type="button" class="back-folder-btn" style="border:none;cursor:pointer;background:var(--caramel);" '
            'onclick="openOffline()">⬇ Save for offline</button>'
        )
        if admin:
            tool_btns += (
                '<button type="button" class="back-folder-btn" style="border:none;cursor:pointer;" '
                'onclick="openAddCourse()">➕ Add Course</button>'
                '<button type="button" class="back-folder-btn" style="border:none;cursor:pointer;" '
                'onclick="openModal(\'bgModal\')">🖼 Background</button>'
            )
        home_tools = f'<div style="display:flex; gap:8px; flex-wrap:wrap; margin:4px 0 4px;">{tool_btns}</div>'
        main_view_content = search_bar + home_tools + folders_grid
        # Background image (main screen only)
        bg_row = SNAP["bg"]
        if bg_row:
            bg_ver = int(bg_row[0] or 1)
            bg_op = max(5, min(40, int(bg_row[1] if bg_row[1] is not None else 15)))
            bg_layer = f'<div id="app-bg" style="background-image:url(\'/bg?v={bg_ver}\'); opacity:{bg_op / 100:.2f};"></div>'
        else:
            bg_op = 15
        if admin:
            preview = (
                f'<img src="/bg?v={bg_ver}" alt="Current background" style="width:100%; max-height:150px; object-fit:cover; border-radius:14px; margin-bottom:12px; opacity:0.8;">'
                if bg_row else '<div class="helper-text" style="margin-bottom:10px;">No background image set yet.</div>'
            )
            remove_form = (
                '<form action="/background/delete" method="POST" style="margin-top:10px;" '
                'onsubmit="return confirm(\'Remove the background image?\');">'
                '<button type="submit" class="btn-pill" style="background:#BA1A1A;">Remove Background</button></form>'
                if bg_row else ""
            )
            bg_modal = f"""
            <div class="modal" id="bgModal" onclick="if(event.target === this) closeModal('bgModal')">
                <div class="modal-content">
                    <div class="modal-title">Main Screen Background</div>
                    {preview}
                    <form action="/background/save" method="POST" enctype="multipart/form-data" onsubmit="showLoader()">
                        <div class="form-group">
                            <label>{"Replace image" if bg_row else "Upload image"} (JPG, PNG or WebP, up to 6 MB)</label>
                            <input type="file" name="file" accept="image/jpeg,image/png,image/webp">
                            <div class="helper-text">Leave empty to keep the current image and only change the opacity.</div>
                        </div>
                        <div class="form-group">
                            <label>Opacity: <span id="bgOpVal">{bg_op}</span>%</label>
                            <input type="range" name="opacity" min="5" max="40" value="{bg_op}" oninput="document.getElementById('bgOpVal').innerText = this.value">
                            <div class="helper-text">Kept faint on purpose (max 40%) so every course stays clearly visible.</div>
                        </div>
                        <button type="submit" class="btn-pill btn-caramel">Save Background</button>
                    </form>
                    {remove_form}
                </div>
            </div>
            """

    # ── LEVEL 2: Course → subject folders (grouped by semester)
    elif course and not subject and not q:
        rows = snap_subcounts(course)
        seeded = CURRICULUM.get(course, {})
        seeded_names = {n for names in seeded.values() for n in names}
        sub_counts, extra = {}, {}
        for subj, sm, n in rows:
            subj = subj or UNSORTED
            sub_counts[subj] = sub_counts.get(subj, 0) + n
            if subj not in seeded_names and subj != UNSORTED:
                extra.setdefault(sm, [])
                if subj not in extra[sm]:
                    extra[sm].append(subj)

        tile = course_tile(course)
        icon = icon_svg(tile["icon"], 26) if tile else icon_svg("folder", 26)
        label = tile["label"] if tile else course
        add_btn = (
            f'<button type="button" class="back-folder-btn" style="border:none;cursor:pointer;background:var(--caramel);" '
            f'data-course="{esc(course)}" onclick="openAddSubject(this)">➕ Add Subject</button>'
            if admin else ""
        )
        rescan_btn = (
            f'<form action="/subject/rescan" method="POST" style="margin:0;">'
            f'<input type="hidden" name="course" value="{esc(course)}">'
            f'<button type="submit" class="back-folder-btn" style="border:none;cursor:pointer;" '
            f'title="Move Unsorted papers into matching subjects">🔄 Re-sort</button></form>'
            if admin else ""
        )
        header_bar = f"""
        <div class="folder-header-bar">
            <div class="folder-header-title"><span>{icon}</span><span>{esc(label)}</span></div>
            <div style="display:flex; gap:8px; align-items:center;">{add_btn}{rescan_btn}<a href="/" class="back-folder-btn">← All Courses</a></div>
        </div>
        """
        search_form = f"""
        <form method="GET" action="/">
            <input type="hidden" name="course" value="{esc(course)}">
            <div class="search-box"><input type="text" name="q" placeholder="🔍 Search papers in {esc(label)}..."></div>
        </form>
        """
        sections = ""
        for sem_label in SEMESTERS[1:]:
            names = seeded.get(sem_label, []) + extra.get(sem_label, [])
            if not names:
                continue
            cards = "".join(subject_card_html(course, n, sub_counts.get(n, 0), admin, sem_label) for n in names)
            sections += f'<div class="sem-heading">{esc(sem_label.replace("Sem", "Semester"))}</div><div class="subject-grid">{cards}</div>'
        if sub_counts.get(UNSORTED):
            sections += (
                '<div class="sem-heading">Not sorted yet</div><div class="subject-grid">'
                + subject_card_html(course, UNSORTED, sub_counts[UNSORTED])
                + "</div>"
            )
        if not sections:
            sections = '<div class="card" style="text-align:center; padding:40px;"><p style="color:var(--text-muted);">No papers in this course yet.</p></div>'
        main_view_content = crumbs_html(course, "", "") + header_bar + search_form + sections

    # ── LEVEL 3: PYQ list (subject folder, course search, or global search)
    else:
        records = snap_records(course, subject, sem, year, q)

        tile = course_tile(course)
        if subject:
            folder_icon = icon_svg(icon_for(course, subject), 26)
            folder_display_name = subject
            back_href = f"/?course={urlq(course)}" if course else "/"
            back_label = "← Subjects" if course else "← All Folders"
        else:
            folder_icon = icon_svg(tile["icon"], 26) if tile else "🔍"
            folder_display_name = (tile["label"] if tile else course) if course else f"Search: {q}"
            back_href = f"/?course={urlq(course)}" if (course and q) else "/"
            back_label = "← Subjects" if (course and q) else "← All Folders"
        header_bar = f"""
        <div class="folder-header-bar">
            <div class="folder-header-title"><span>{folder_icon}</span><span>{esc(folder_display_name)}</span></div>
            <a href="{esc(back_href)}" class="back-folder-btn">{back_label}</a>
        </div>
        """
        if subject:
            # Inside a subject the semester is already known, so filter by exam year instead
            year_rows = [(y,) for y in snap_years(course, subject)]
            years = sorted(
                {r[0] for r in year_rows if r[0]},
                key=lambda v: (not v.isdigit(), -int(v) if v.isdigit() else 0, v),
            )
            year_options = '<option value="All Years">All Years</option>' + "".join(
                f'<option value="{esc(v)}" {"selected" if v == year else ""}>{esc(v)}</option>' for v in years
            )
            filter_select = f'<select name="year" onchange="this.form.submit()">{year_options}</select>'
            search_placeholder = f"🔍 Search in {subject}..."
        else:
            sem_options = "".join(
                f'<option value="{esc(s)}" {"selected" if s == sem else ""}>{esc(s)}</option>' for s in SEMESTERS
            )
            filter_select = f'<select name="sem" onchange="this.form.submit()">{sem_options}</select>'
            search_placeholder = "🔍 Live search in this list..."
        search_filter_form = f"""
        <form method="GET" action="/">
            <input type="hidden" name="course" value="{esc(course)}">
            <input type="hidden" name="subject" value="{esc(subject)}">
            <div class="search-box">
                <input type="text" name="q" value="{esc(q)}" placeholder="{esc(search_placeholder)}" onkeyup="filterCardsLive(this.value)" autocomplete="off">
            </div>
            <div class="filters">{filter_select}</div>
        </form>
        """
        if not records:
            cards_html = '<div class="card" style="text-align:center; padding:40px;"><p style="color:var(--text-muted);">No papers found here yet.</p></div>'
        else:
            cards_html = '<div class="cards-layout-grid">'
            for item_id, title, c, s, y, r_type, url_or_name, size, subj in records:
                subj = subj or UNSORTED
                if r_type == "pdf":
                    action_btn = (
                        f'<div style="margin-top: 10px;"><button class="btn-pill" '
                        f'data-url="/view/{item_id}" data-title="{esc(title)}" '
                        f'onclick="openPdfViewer(this.dataset.url, this.dataset.title)">View</button></div>'
                    )
                else:
                    href = url_or_name if safe_http_url(url_or_name or "") else "#"
                    action_btn = (
                        f'<div style="margin-top: 10px;"><a class="btn-pill btn-caramel" href="{esc(href)}" '
                        f'target="_blank" rel="noopener noreferrer">Open Link</a></div>'
                    )
                admin_opts = ""
                if admin:
                    admin_opts = f"""
                    <div class="admin-actions">
                        <button class="edit-btn" data-id="{item_id}" data-title="{esc(title)}" data-course="{esc(c)}"
                            data-sem="{esc(s)}" data-year="{esc(y)}" data-subject="{esc(subj)}"
                            onclick="openEditModal(this)">Edit</button>
                        <form action="/delete/{item_id}" method="POST" onsubmit="return confirm('Delete paper?');">
                            <button type="submit" class="del-btn">✕</button>
                        </form>
                    </div>
                    """
                search_data = esc(f"{title} {c} {s} {y or ''} {subj}".lower())
                cards_html += f"""
                <div class="card card-item" data-search-text="{search_data}">
                    <div>
                        <div class="card-top">
                            <span class="badge">{esc(subj if not subject else c)}</span>
                            {admin_opts}
                        </div>
                        <div class="card-title">{esc(title)}</div>
                        <div class="card-meta">{esc(s)} &nbsp; &nbsp; {esc(y or "")} &nbsp; &nbsp; {esc(size or "")}</div>
                    </div>
                    {action_btn}
                </div>
                """
            cards_html += "</div>"
        main_view_content = (
            crumbs_html(course, subject, q) + header_bar + search_filter_form + f'<div id="cards-container">{cards_html}</div>'
        )

    if notice:
        main_view_content = f'<div class="notice">{esc(notice[:200])}</div>' + main_view_content

    # Subject suggestions for the upload / edit forms
    known = snap_known_subjects()
    for sems in CURRICULUM.values():
        for names in sems.values():
            known.update(names)
    known.discard(UNSORTED)
    subject_datalist = "".join(f'<option value="{esc(n)}"></option>' for n in sorted(known))

    up_course_opts = "".join(f'<option value="{esc(c)}">{esc(c)}</option>' for c in course_names())
    up_sem_opts = "".join(f'<option value="{esc(s)}">{esc(s)}</option>' for s in SEMESTERS[1:])
    if admin:
        admin_header_btn = '<a href="/logout" class="admin-lock-btn">Logout</a>'
        admin_banner_html = '<div class="admin-banner"><span>⚡ Host Controls Unlocked</span><span><a href="/diag">Speed check</a> &nbsp;&nbsp; <a href="/logout">Lock</a></span></div>'
        fab_controls = """
        <div class="fab-bar">
            <button class="fab" onclick="openModal('uploadModal')">📤 Batch Upload</button>
            <button class="fab" style="background: var(--caramel);" onclick="openModal('linkModal')">🔗 Add Link</button>
        </div>
        """
    else:
        admin_header_btn = '<button onclick="openModal(\'loginModal\')" class="admin-lock-btn">Admin</button>'
        admin_banner_html = ""
        fab_controls = ""
    content = HTML_TEMPLATE
    # main_view_content is replaced LAST so user-entered text can never be treated as a placeholder
    content = content.replace("{upload_course_options}", up_course_opts)
    content = content.replace("{upload_sem_options}", up_sem_opts)
    content = content.replace("{subject_datalist}", subject_datalist)
    content = content.replace("{gradients_json}", json.dumps(GRADIENTS))
    content = content.replace("{icons_json}", json.dumps(ICON_LIBRARY))
    content = content.replace("{bg_layer}", bg_layer)
    content = content.replace("{bg_modal}", bg_modal)
    content = content.replace("{admin_header_btn}", admin_header_btn)
    content = content.replace("{admin_banner_html}", admin_banner_html)
    content = content.replace("{fab_controls}", fab_controls)
    content = content.replace("{main_view_content}", main_view_content)
    return content


# ───────────────────────── AUTH ROUTES ─────────────────────────
_LOGIN_FAILS = {}


def client_ip(request: Request) -> str:
    fwd = request.headers.get("x-forwarded-for", "")
    return (fwd.split(",")[0].strip() if fwd else "") or (request.client.host if request.client else "?")


@app.post("/login")
def login(request: Request, password: str = Form(...)):
    ip = client_ip(request)
    recent = [t for t in _LOGIN_FAILS.get(ip, []) if time.time() - t < 600]
    _LOGIN_FAILS[ip] = recent
    if len(recent) >= 6:
        return HTMLResponse("Too many wrong attempts. Please wait 10 minutes and try again.", status_code=429)
    if hmac.compare_digest(password.encode(), ADMIN_PASSWORD.encode()):
        _LOGIN_FAILS.pop(ip, None)
        response = RedirectResponse(url="/?fresh=1", status_code=303)
        response.set_cookie(
            key="du_admin_session",
            value=make_session_token(),
            httponly=True,
            secure=COOKIE_SECURE,
            samesite="lax",
            max_age=SESSION_TTL,
        )
        return response
    _LOGIN_FAILS[ip].append(time.time())
    return HTMLResponse("Invalid Password", status_code=401)


@app.get("/logout")
def logout():
    response = RedirectResponse(url="/?fresh=1", status_code=303)
    response.delete_cookie(key="du_admin_session")
    return response


# ───────────────────────── ADMIN ACTIONS ─────────────────────────
def folder_redirect(course: str, subject: str = "", notice: str = "") -> RedirectResponse:
    url = f"/?course={urlq(course)}"
    if subject:
        url += f"&subject={urlq(subject)}"
    if notice:
        url += f"&notice={urlq(notice)}"
    else:
        url += "&fresh=1"
    return RedirectResponse(url=url, status_code=303)


@app.post("/upload")
async def upload_files(
    request: Request,
    course: str = Form("auto"),
    sem: str = Form("auto"),
    year: str = Form(""),
    subject: str = Form(""),
    files: list[UploadFile] = File(...),
):
    if not is_admin(request):
        return HTMLResponse("Unauthorized. Please log in as Admin.", status_code=403)
    if course != "auto" and course not in course_names():
        course = "auto"
    if sem != "auto" and sem not in SEMESTERS[1:]:
        sem = "auto"
    conn = get_db()
    cursor = conn.cursor()
    last_course, last_subject = None, ""
    skipped = []
    for file in files:
        if file.filename and file.filename.lower().endswith(".pdf"):
            data = await file.read()
            if len(data) > MAX_PDF_BYTES:
                skipped.append(file.filename)
                continue
            size_mb = f"{len(data) / (1024 * 1024):.2f} MB"
            clean_title, detected_course, detected_sem, detected_year = parse_filename(file.filename, course, sem, year)
            detected_subject = resolve_subject(detected_course, detected_sem, subject, file.filename)
            last_course, last_subject = detected_course, detected_subject
            ensure_subject(cursor, detected_course, detected_sem, detected_subject)
            cursor.execute(
                """
                INSERT INTO du_resources (title, course, semester, year, type, url_or_name, file_data, file_size, subject)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (clean_title, detected_course, detected_sem, detected_year, "pdf", file.filename, data, size_mb, detected_subject),
            )
    conn.commit()
    conn.close()
    skip_note = (f"Skipped (over {MAX_PDF_BYTES // 1048576} MB): " + ", ".join(skipped))[:180] if skipped else ""
    if not last_course:
        return RedirectResponse(url="/?" + (f"notice={urlq(skip_note)}" if skip_note else "fresh=1"), status_code=303)
    # Single subject in the batch → open that subject; otherwise open the course
    return folder_redirect(last_course, last_subject if subject.strip() else "", notice=skip_note)


@app.post("/edit-item")
def edit_item(
    request: Request,
    item_id: int = Form(...),
    title: str = Form(...),
    course: str = Form(...),
    sem: str = Form(...),
    year: str = Form(...),
    subject: str = Form(""),
):
    if not is_admin(request):
        return HTMLResponse("Unauthorized.", status_code=403)
    if course not in course_names() or sem not in SEMESTERS[1:]:
        return HTMLResponse("Invalid course or semester.", status_code=400)
    final_subject = resolve_subject(course, sem, subject, title)
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute(
        """
        UPDATE du_resources
        SET title = ?, course = ?, semester = ?, year = ?, subject = ?
        WHERE id = ?
        """,
        (title, course, sem, year, final_subject, item_id),
    )
    ensure_subject(cursor, course, sem, final_subject)
    conn.commit()
    conn.close()
    return folder_redirect(course, final_subject)


@app.post("/add-link")
def add_link(
    request: Request,
    title: str = Form(...),
    url: str = Form(...),
    course: str = Form(...),
    sem: str = Form(...),
    subject: str = Form(""),
):
    if not is_admin(request):
        return HTMLResponse("Unauthorized.", status_code=403)
    if not safe_http_url(url):
        return HTMLResponse("Only http:// or https:// links are allowed.", status_code=400)
    if course not in course_names() or sem not in SEMESTERS[1:]:
        return HTMLResponse("Invalid course or semester.", status_code=400)
    final_subject = resolve_subject(course, sem, subject, title)
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute(
        """
        INSERT INTO du_resources (title, course, semester, year, type, url_or_name, file_data, file_size, subject)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (title, course, sem, "Web", "link", url.strip(), None, "Link", final_subject),
    )
    ensure_subject(cursor, course, sem, final_subject)
    conn.commit()
    conn.close()
    return folder_redirect(course, final_subject)


def clean_subject_name(name: str) -> str:
    return " ".join(name.split())[:80]


def subject_exists(course: str, name: str, ignore: str = "") -> bool:
    for names in CURRICULUM.get(course, {}).values():
        for n in names:
            if n.casefold() == name.casefold() and n.casefold() != ignore.casefold():
                return True
    return False


def clean_icon(icon: str) -> str:
    return (icon or "").strip()[:16]


def clean_color(color: str) -> str:
    color = (color or "").strip()
    return color if color.isdigit() and int(color) < len(GRADIENTS) else ""


def sort_papers(cursor, course: str, only: str = None, include_sorted: bool = False) -> int:
    """Re-run name matching on a course's papers. Normally only touches 'Unsorted' ones."""
    sql = "SELECT id, title, url_or_name, semester, subject, type FROM du_resources WHERE course = ?"
    params = [course]
    if not include_sorted:
        sql += " AND (subject IS NULL OR subject = '' OR subject = ?)"
        params.append(UNSORTED)
    cursor.execute(sql, tuple(params))
    moved = 0
    for pid, title, fname, psem, cur_sub, rtype in cursor.fetchall():
        found = detect_subject(course, psem, f"{title} {fname if rtype == 'pdf' else ''}")
        if found == UNSORTED or found == cur_sub or (only and found != only):
            continue
        cursor.execute("UPDATE du_resources SET subject = ? WHERE id = ?", (found, pid))
        moved += 1
    return moved


def clean_text(value: str, limit: int) -> str:
    return " ".join((value or "").split())[:limit]


def course_by_name(name: str):
    return next((c for c in COURSE_LIST if c["name"] == name), None)


def course_name_taken(name: str, ignore: str = "") -> bool:
    return any(c["name"].casefold() == name.casefold() and c["name"] != ignore for c in COURSE_LIST)


@app.post("/course/add")
def course_add(
    request: Request,
    name: str = Form(...),
    label: str = Form(""),
    icon: str = Form(""),
    color: str = Form(""),
):
    if not is_admin(request):
        return HTMLResponse("Unauthorized.", status_code=403)
    name = clean_text(name, 80)
    label = clean_text(label, 40) or name[:40]
    if not name or name.casefold() == "all courses":
        return HTMLResponse("Please enter a valid course name.", status_code=400)
    conn = get_db()
    cursor = conn.cursor()
    reload_courses(cursor)
    if course_name_taken(name):
        conn.close()
        return HTMLResponse("A course with this name already exists.", status_code=400)
    cursor.execute("SELECT COALESCE(MAX(position), 0) + 1 FROM du_courses")
    pos = cursor.fetchone()[0]
    cursor.execute("SELECT COUNT(*) FROM du_courses WHERE key = ?", (name,))
    key = name if cursor.fetchone()[0] == 0 else f"{name}#{pos}"
    cursor.execute(
        "INSERT INTO du_courses (key, name, label, icon, color, position) VALUES (?, ?, ?, ?, ?, ?)",
        (key, name, label, clean_icon(icon) or "folder", clean_color(color), pos),
    )
    conn.commit()
    reload_courses(cursor)
    conn.close()
    return RedirectResponse(url=f"/?notice={urlq(f'Course added: {name}')}", status_code=303)


@app.post("/course/edit")
def course_edit(
    request: Request,
    old_name: str = Form(...),
    name: str = Form(...),
    label: str = Form(""),
    icon: str = Form(""),
    color: str = Form(""),
):
    if not is_admin(request):
        return HTMLResponse("Unauthorized.", status_code=403)
    name = clean_text(name, 80)
    label = clean_text(label, 40) or name[:40]
    if not name or name.casefold() == "all courses":
        return HTMLResponse("Please enter a valid course name.", status_code=400)
    conn = get_db()
    cursor = conn.cursor()
    reload_courses(cursor)
    if not course_by_name(old_name):
        conn.close()
        return HTMLResponse("Course not found.", status_code=404)
    if course_name_taken(name, ignore=old_name):
        conn.close()
        return HTMLResponse("Another course already has that name.", status_code=400)
    cursor.execute(
        "UPDATE du_courses SET name = ?, label = ?, icon = ?, color = ? WHERE name = ?",
        (name, label, clean_icon(icon) or "folder", clean_color(color), old_name),
    )
    if name != old_name:
        cursor.execute("UPDATE du_resources SET course = ? WHERE course = ?", (name, old_name))
        cursor.execute("UPDATE du_subjects SET course = ? WHERE course = ?", (name, old_name))
    conn.commit()
    reload_courses(cursor)
    reload_curriculum(cursor)
    conn.close()
    return RedirectResponse(url=f"/?notice={urlq('Course saved.')}", status_code=303)


@app.post("/course/delete")
def course_delete(request: Request, name: str = Form(...)):
    """Deletes the course folder and its subject folders. Papers are kept and moved to 'General / Other'."""
    if not is_admin(request):
        return HTMLResponse("Unauthorized.", status_code=403)
    conn = get_db()
    cursor = conn.cursor()
    reload_courses(cursor)
    target = course_by_name(name)
    general = next((c for c in COURSE_LIST if c["key"] == GENERAL_KEY), None)
    if not target:
        conn.close()
        return HTMLResponse("Course not found.", status_code=404)
    if target["key"] == GENERAL_KEY or general is None:
        conn.close()
        return HTMLResponse("'General / Other' is the safety folder and can't be deleted.", status_code=400)
    cursor.execute("SELECT COUNT(*) FROM du_resources WHERE course = ?", (name,))
    n = cursor.fetchone()[0]
    cursor.execute("UPDATE du_resources SET course = ?, subject = ? WHERE course = ?", (general["name"], UNSORTED, name))
    cursor.execute("DELETE FROM du_subjects WHERE course = ?", (name,))
    cursor.execute("DELETE FROM du_courses WHERE name = ?", (name,))
    conn.commit()
    reload_courses(cursor)
    reload_curriculum(cursor)
    conn.close()
    note = f"Course deleted. {n} paper(s) moved to {general['name']}." if n else "Course deleted."
    return RedirectResponse(url=f"/?notice={urlq(note)}", status_code=303)


# ───────────── routes: main-screen background ─────────────
BG_MAX_BYTES = 6 * 1024 * 1024


def sniff_image(data: bytes):
    if data[:3] == b"\xff\xd8\xff":
        return "image/jpeg"
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    return None


def prepare_background(data: bytes):
    """Validate by file signature, then shrink to max 1920px JPEG if Pillow is installed."""
    mime = sniff_image(data)
    if not mime:
        return None
    try:
        from PIL import Image
    except ImportError:
        return (data, mime) if len(data) <= 3 * 1024 * 1024 else None
    try:
        img = Image.open(io.BytesIO(data))
        img.load()
        img = img.convert("RGB")
        img.thumbnail((1920, 1920))
        out = io.BytesIO()
        img.save(out, "JPEG", quality=80, optimize=True)
        return out.getvalue(), "image/jpeg"
    except Exception:
        return None


@app.get("/bg")
def get_background(v: int = 0):
    item = blob_get(("bg", v))
    if item is None:
        rows = _q("SELECT data, mime FROM du_background WHERE id = 1")
        row = rows[0] if rows else None
        if not row or not row[0]:
            return HTMLResponse("No background", status_code=404)
        item = (row[1] or "image/jpeg", bytes(row[0]))
        blob_put(("bg", v), item, len(item[1]))
    return Response(
        content=item[1],
        media_type=item[0],
        headers={"Cache-Control": "public, max-age=31536000, immutable", "X-Content-Type-Options": "nosniff"},
    )


@app.post("/background/save")
async def background_save(
    request: Request,
    opacity: int = Form(15),
    file: UploadFile = File(None),
):
    if not is_admin(request):
        return HTMLResponse("Unauthorized.", status_code=403)
    opacity = max(5, min(40, opacity))
    new_image = None
    if file is not None and file.filename:
        data = await file.read()
        if len(data) > BG_MAX_BYTES:
            return HTMLResponse("Image is too large (max 6 MB).", status_code=400)
        new_image = prepare_background(data)
        if not new_image:
            return HTMLResponse(
                "Please upload a valid JPG, PNG or WebP image (without Pillow installed, up to 3 MB).", status_code=400
            )
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("SELECT version FROM du_background WHERE id = 1")
    row = cursor.fetchone()
    if new_image:
        blob, mime = new_image
        if row:
            cursor.execute(
                "UPDATE du_background SET data = ?, mime = ?, opacity = ?, version = ? WHERE id = 1",
                (blob, mime, opacity, int(row[0] or 1) + 1),
            )
        else:
            cursor.execute(
                "INSERT INTO du_background (id, data, mime, opacity, version) VALUES (1, ?, ?, ?, 1)",
                (blob, mime, opacity),
            )
        note = "Background saved."
    elif row:
        cursor.execute("UPDATE du_background SET opacity = ? WHERE id = 1", (opacity,))
        note = "Background opacity updated."
    else:
        conn.close()
        return HTMLResponse("Choose an image to upload first.", status_code=400)
    conn.commit()
    conn.close()
    return RedirectResponse(url=f"/?notice={urlq(note)}", status_code=303)


@app.post("/background/delete")
def background_delete(request: Request):
    if not is_admin(request):
        return HTMLResponse("Unauthorized.", status_code=403)
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("DELETE FROM du_background WHERE id = 1")
    conn.commit()
    conn.close()
    return RedirectResponse(url=f"/?notice={urlq('Background removed.')}", status_code=303)


@app.post("/subject/add")
def subject_add(
    request: Request,
    course: str = Form(...),
    sem: str = Form(...),
    name: str = Form(...),
    icon: str = Form(""),
    color: str = Form(""),
    pull_from_others: str = Form(""),
):
    if not is_admin(request):
        return HTMLResponse("Unauthorized.", status_code=403)
    name = clean_subject_name(name)
    if course not in course_names() or sem not in SEMESTERS[1:] or not name or name.casefold() == UNSORTED.casefold():
        return HTMLResponse("Invalid course, semester or subject name.", status_code=400)
    conn = get_db()
    cursor = conn.cursor()
    reload_curriculum(cursor)
    if subject_exists(course, name):
        conn.close()
        return HTMLResponse("A subject with this name already exists in this course.", status_code=400)
    cursor.execute(
        "INSERT INTO du_subjects (course, semester, name, icon, color) VALUES (?, ?, ?, ?, ?)",
        (course, sem, name, clean_icon(icon), clean_color(color)),
    )
    conn.commit()
    reload_curriculum(cursor)
    moved = sort_papers(cursor, course, only=name, include_sorted=bool(pull_from_others))
    conn.commit()
    conn.close()
    notice = (
        f"Added '{name}' and moved {moved} matching paper(s) into it."
        if moved else f"Added '{name}'. No existing papers matched its name."
    )
    return folder_redirect(course, notice=notice)


@app.post("/subject/edit")
def subject_edit(
    request: Request,
    course: str = Form(...),
    old_name: str = Form(...),
    new_name: str = Form(...),
    sem: str = Form(...),
    icon: str = Form(""),
    color: str = Form(""),
):
    if not is_admin(request):
        return HTMLResponse("Unauthorized.", status_code=403)
    new_name = clean_subject_name(new_name)
    if course not in course_names() or sem not in SEMESTERS[1:] or not new_name or new_name.casefold() == UNSORTED.casefold():
        return HTMLResponse("Invalid course, semester or subject name.", status_code=400)
    conn = get_db()
    cursor = conn.cursor()
    reload_curriculum(cursor)
    if subject_exists(course, new_name, ignore=old_name):
        conn.close()
        return HTMLResponse("Another subject in this course already has that name.", status_code=400)
    cursor.execute(
        "UPDATE du_subjects SET name = ?, semester = ?, icon = ?, color = ? WHERE course = ? AND name = ?",
        (new_name, sem, clean_icon(icon), clean_color(color), course, old_name),
    )
    if new_name != old_name:
        cursor.execute(
            "UPDATE du_resources SET subject = ? WHERE course = ? AND subject = ?",
            (new_name, course, old_name),
        )
    conn.commit()
    reload_curriculum(cursor)
    moved = sort_papers(cursor, course, only=new_name)
    conn.commit()
    conn.close()
    return folder_redirect(course, notice=f"Saved. {moved} unsorted paper(s) matched '{new_name}'." if moved else "")


@app.post("/subject/rescan")
def subject_rescan(request: Request, course: str = Form(...)):
    """Try again to place every 'Unsorted' paper of this course into a matching subject."""
    if not is_admin(request):
        return HTMLResponse("Unauthorized.", status_code=403)
    conn = get_db()
    cursor = conn.cursor()
    reload_curriculum(cursor)
    moved = sort_papers(cursor, course)
    conn.commit()
    conn.close()
    return folder_redirect(course, notice=f"Re-sorted: {moved} paper(s) moved into matching subjects.")


@app.post("/subject/delete")
def subject_delete(request: Request, course: str = Form(...), name: str = Form(...)):
    """Deletes the subject folder only. Its papers are kept and moved to 'Unsorted'."""
    if not is_admin(request):
        return HTMLResponse("Unauthorized.", status_code=403)
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("UPDATE du_resources SET subject = ? WHERE course = ? AND subject = ?", (UNSORTED, course, name))
    cursor.execute("DELETE FROM du_subjects WHERE course = ? AND name = ?", (course, name))
    conn.commit()
    reload_curriculum(cursor)
    conn.close()
    return folder_redirect(course)


@app.get("/api/catalog")
def api_catalog():
    """List of pages and papers, used by the 'Save for offline' button and the background warm-up."""
    ensure_snapshot()

    def build():
        pages = ["/"] + [f"/?course={urlq(c['name'])}" for c in COURSE_LIST]
        pairs, papers = set(), []
        for r in SNAP["papers"]:
            if r[2] and r[8]:
                pairs.add((r[2], r[8]))
            if r[5] == "pdf":
                papers.append({"id": r[0], "title": r[1], "bytes": parse_mb(r[7])})
        pages += [f"/?course={urlq(c)}&subject={urlq(sb)}" for c, sb in sorted(pairs)]
        return {"pages": pages, "papers": papers, "total_bytes": sum(x["bytes"] for x in papers)}

    return JSONResponse(cached(("catalog",), build), headers={"Cache-Control": "no-store"})


@app.get("/diag", response_class=HTMLResponse)
def diag(request: Request):
    """Admin-only speed check: shows where the time really goes (database distance vs. everything else)."""
    if not is_admin(request):
        return HTMLResponse("Unauthorized.", status_code=403)
    t = time.perf_counter()
    raw = _open_raw()
    connect_ms = (time.perf_counter() - t) * 1000
    cursor = raw.cursor()
    t = time.perf_counter()
    cursor.execute("SELECT 1")
    cursor.fetchall()
    first_ms = (time.perf_counter() - t) * 1000
    samples = []
    for _ in range(4):
        t = time.perf_counter()
        cursor.execute("SELECT 1")
        cursor.fetchall()
        samples.append((time.perf_counter() - t) * 1000)
    t = time.perf_counter()
    cursor.execute("SELECT COUNT(*), COALESCE(SUM(LENGTH(file_data)), 0) FROM du_resources")
    n, total = cursor.fetchone()
    count_ms = (time.perf_counter() - t) * 1000
    try:
        raw.close()
    except Exception:
        pass
    avg = sum(samples) / len(samples)
    remote = bool(TURSO_DB_URL and TURSO_AUTH_TOKEN)
    if not remote:
        verdict = "Using a local database file (no Turso variables set). Papers saved this way are lost whenever Render redeploys."
    elif avg < 40:
        verdict = "Database is close and fast. If pages still feel slow, the cause is the phone's network or Render's small CPU."
    elif avg < 120:
        verdict = "Database speed is acceptable. Moving Render and Turso to the same region could still help."
    else:
        verdict = "Database is far from the app (each question takes a long round trip). Pages are now served from memory so visitors are no longer slowed by this, but admin actions and the first opening of a PDF still are. Put the Render service and the Turso database in the same region to fix that too."
    if remote and first_ms > 3 * avg + 200:
        verdict += f" Note: a new connection costs about {first_ms:.0f} ms for its first question; the app now keeps connections open so this is only paid once per worker."
    rows = [
        ("Database", "Turso (remote)" if remote else "Local file"),
        ("Time to open a database connection", f"{connect_ms:.0f} ms"),
        ("First question on a brand-new connection", f"{first_ms:.0f} ms"),
        ("Later questions on the same connection (4 tests)", f"{avg:.0f} ms on average (fastest {min(samples):.0f}, slowest {max(samples):.0f})"),
        ("Heavy query (count + total PDF size)", f"{count_ms:.0f} ms"),
        ("Papers stored / total PDF size", f"{n} / {int(total) / 1048576:.1f} MB"),
        ("Catalogue kept in memory", f"{len(SNAP['papers'])} papers, last refreshed {int(time.time() - SNAP['at'])} s ago"),
        ("Pages kept in memory", f"{len(_CACHE)} items"),
        ("PDFs kept in memory", f"{len(_BLOBS)} files, {_BLOB_BYTES / 1048576:.1f} MB"),
    ]
    table = "".join(f"<tr><td>{esc(k)}</td><td><b>{esc(v)}</b></td></tr>" for k, v in rows)
    return HTMLResponse(f"""<!DOCTYPE html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Speed check</title><style>body{{font-family:system-ui,sans-serif;background:#E8DDD1;color:#261E19;margin:0;padding:20px}}
.box{{max-width:620px;margin:0 auto;background:#FBF8F5;border:1px solid #D8C7B6;border-radius:18px;padding:22px}}
td{{padding:8px 6px;border-bottom:1px solid #E8DDD1;font-size:.92rem}}a{{color:#A77A53;font-weight:600}}.v{{background:#EAF3E3;border-radius:12px;padding:12px 14px;margin:14px 0;font-weight:600}}</style></head>
<body><div class="box"><h2 style="margin-top:0">Speed check</h2><table style="width:100%;border-collapse:collapse">{table}</table>
<div class="v">{esc(verdict)}</div>
<p style="font-size:.85rem;color:#7E6A5B">Tip: in your browser's Network tab, every response has a <b>Server-Timing</b> value showing how long the server itself took. Requests slower than 1.5 s are also written to the Render logs as "SLOW".</p>
<a href="/">← Back</a></div></body></html>""")


def get_pdf(item_id: int):
    item = blob_get(("pdf", item_id))
    if item:
        return item
    rows = _q("SELECT title, file_data FROM du_resources WHERE id = ?", (item_id,))
    row = rows[0] if rows else None
    if not row or not row[1]:
        return None
    item = (row[0], bytes(row[1]))
    blob_put(("pdf", item_id), item, len(item[1]))
    return item


def parse_range(header: str, size: int):
    """None = no usable Range header (send everything); 'bad' = unsatisfiable; else (start, end)."""
    m = re.fullmatch(r"bytes=(\d*)-(\d*)", (header or "").strip())
    if not m or (m.group(1) == "" and m.group(2) == ""):
        return None
    start_s, end_s = m.groups()
    if start_s == "":
        length = int(end_s)
        if length == 0:
            return "bad"
        start, end = max(size - length, 0), size - 1
    else:
        start = int(start_s)
        end = min(int(end_s), size - 1) if end_s else size - 1
    if start > end or start >= size:
        return "bad"
    return start, end


@app.get("/view/{item_id}")
def view_pdf(item_id: int, request: Request):
    item = get_pdf(item_id)
    if not item:
        return HTMLResponse("Not Found", status_code=404)
    title, data = item
    size = len(data)
    etag = f'"pdf-{item_id}-{size}"'
    cache_control = "public, max-age=604800, immutable"
    if request.headers.get("if-none-match") == etag:
        return Response(status_code=304, headers={"ETag": etag, "Cache-Control": cache_control})
    headers = {
        "Content-Disposition": f"inline; filename*=UTF-8''{urllib.parse.quote(title + '.pdf')}",
        "X-Content-Type-Options": "nosniff",
        "Cache-Control": cache_control,
        "ETag": etag,
        "Accept-Ranges": "bytes",
        "Content-Encoding": "identity",  # never gzip PDFs: it breaks partial downloads
    }
    rng = parse_range(request.headers.get("range", ""), size)
    if rng == "bad":
        return Response(status_code=416, headers={"Content-Range": f"bytes */{size}"})
    if rng:
        start, end = rng
        headers["Content-Range"] = f"bytes {start}-{end}/{size}"
        return Response(content=data[start:end + 1], status_code=206, media_type="application/pdf", headers=headers)
    return Response(content=data, media_type="application/pdf", headers=headers)


@app.post("/delete/{item_id}")
def delete_item(request: Request, item_id: int):
    if not is_admin(request):
        return HTMLResponse("Unauthorized.", status_code=403)
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("DELETE FROM du_resources WHERE id = ?", (item_id,))
    conn.commit()
    conn.close()
    return RedirectResponse(url=add_fresh(back_url(request)), status_code=303)


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
