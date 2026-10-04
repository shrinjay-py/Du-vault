import os
import io
import re
import urllib.parse
import libsql_experimental as sqlite3
from fastapi import FastAPI, UploadFile, File, Form, Response, Request
from fastapi.responses import HTMLResponse, StreamingResponse, RedirectResponse
from fastapi.middleware.gzip import GZipMiddleware

app = FastAPI(title="DU PYQ Vault")
app.add_middleware(GZipMiddleware, minimum_size=1000)

TURSO_DB_URL = os.getenv("TURSO_DB_URL")
TURSO_AUTH_TOKEN = os.getenv("TURSO_AUTH_TOKEN")
ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD", "admin123")

def get_db():
    if TURSO_DB_URL and TURSO_AUTH_TOKEN:
        return sqlite3.connect(TURSO_DB_URL, auth_token=TURSO_AUTH_TOKEN)
    else:
        return sqlite3.connect("du_pyq_vault.db")

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
    "General / Other"
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
    {"name": "B.A. Programme", "icon": "📚", "label": "B.A. Prog"},
    {"name": "B.Sc (prog) Physical science with Chemistry", "icon": "🧲", "label": "Physical science with chem"}
]

SEMESTERS = ["All Semesters", "Sem 1", "Sem 2", "Sem 3", "Sem 4", "Sem 5", "Sem 6"]

CATEGORY_MAP = {
    "pyq": {"label": "📄 PYQs", "icon": "📄", "name": "Previous Year Questions"},
    "notes": {"label": "📝 Notes", "icon": "📝", "name": "Notes & Study Material"},
    "practical": {"label": "🔬 Practical", "icon": "🔬", "name": "Practical Files & Lab Manuals"},
    "timetable": {"label": "📅 Timetable", "icon": "📅", "name": "Timetables & Schedules"}
}

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
            category TEXT DEFAULT 'pyq',
            url_or_name TEXT,
            file_data BLOB,
            file_size TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    try:
        cursor.execute("ALTER TABLE du_resources ADD COLUMN category TEXT DEFAULT 'pyq'")
    except Exception:
        pass
    conn.commit()
    conn.close()

init_db()

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

SERVICE_WORKER_JS = """const CACHE_NAME = 'du-vault-cache-v16';
const PRECACHE = [
  '/', 
  '/manifest.json', 
  '/icon.svg',
  'https://cdnjs.cloudflare.com/ajax/libs/pdf.js/3.11.174/pdf.min.js',
  'https://cdnjs.cloudflare.com/ajax/libs/pdf.js/3.11.174/pdf.worker.min.js'
];

self.addEventListener('install', (e) => {
  e.waitUntil(caches.open(CACHE_NAME).then((c) => c.addAll(PRECACHE)));
  self.skipWaiting();
});

self.addEventListener('activate', (e) => {
  e.waitUntil(
    caches.keys().then((keys) =>
      Promise.all(keys.map((k) => (k !== CACHE_NAME ? caches.delete(k) : null)))
    )
  );
  self.clients.claim();
});

self.addEventListener('fetch', (e) => {
  if (e.request.method !== 'GET') return;
  if (e.request.url.includes('/view/')) {
    e.respondWith(
      caches.open(CACHE_NAME).then(async (cache) => {
        const cachedResponse = await cache.match(e.request);
        if (cachedResponse) return cachedResponse;
        const netResponse = await fetch(e.request);
        cache.put(e.request, netResponse.clone());
        return netResponse;
      })
    );
    return;
  }
  e.respondWith(
    fetch(e.request).catch(async () => {
      const cached = await caches.match(e.request);
      return cached || (e.request.mode === 'navigate' ? caches.match('/') : null);
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

    <link href="https://fonts.googleapis.com/css2?family=Georgia&family=Inter:wght@400;500;600;700&display=swap" rel="stylesheet">
    <script src="https://cdnjs.cloudflare.com/ajax/libs/pdf.js/3.11.174/pdf.min.js"></script>

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
        div[style*="z-index"][style*="fixed"] a[href*="render.com"] {
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
        #loading-screen.fade-out {
            opacity: 0;
            visibility: hidden;
            pointer-events: none;
        }
        .loader-box {
            position: relative;
            width: 90px;
            height: 90px;
            display: flex;
            align-items: center;
            justify-content: center;
            margin-bottom: 16px;
        }
        .loader-ring {
            position: absolute;
            width: 100%;
            height: 100%;
            border-radius: 50%;
            border: 3px solid rgba(167, 122, 83, 0.2);
            border-top: 3px solid var(--caramel);
            animation: spinRing 1s cubic-bezier(0.55, 0.055, 0.675, 0.19) infinite;
        }
        .loader-logo {
            width: 56px;
            height: 56px;
            background: var(--caramel);
            border-radius: 14px;
            display: flex;
            align-items: center;
            justify-content: center;
            color: #FAF6F2;
            font-family: 'Georgia', serif;
            font-weight: bold;
            font-size: 1.3rem;
            box-shadow: 0 4px 18px rgba(167, 122, 83, 0.35);
            animation: pulseLogo 1.6s ease-in-out infinite alternate;
        }
        .loader-text {
            color: #FAF6F2;
            font-family: 'Georgia', serif;
            font-size: 1rem;
            letter-spacing: 1px;
        }
        .loader-subtext {
            color: #A77A53;
            font-size: 0.72rem;
            letter-spacing: 1.5px;
            text-transform: uppercase;
            font-weight: 600;
            margin-top: 6px;
        }
        @keyframes spinRing { 0% { transform: rotate(0deg); } 100% { transform: rotate(360deg); } }
        @keyframes pulseLogo { 0% { transform: scale(0.94); } 100% { transform: scale(1.05); } }

        header {
            background: var(--espresso);
            color: #FAF6F2;
            padding: 14px 24px;
            position: sticky; top: 0; z-index: 50;
            display: flex;
            align-items: center;
            justify-content: space-between;
        }
        .brand-title { 
            font-family: 'Georgia', serif; 
            font-size: 1.25rem; 
            font-weight: bold; 
            letter-spacing: 0.5px;
            text-decoration: none;
            color: #FAF6F2;
        }
        .brand-right {
            display: flex;
            align-items: center;
            gap: 16px;
        }
        .founder-tag {
            font-family: 'Georgia', serif;
            font-size: 0.9rem;
            color: #FAF6F2;
            text-align: right;
            line-height: 1.2;
        }
        .founder-tag span.founder-name {
            color: var(--caramel);
            font-weight: 700;
            font-size: 0.95rem;
        }
        .college-subtag {
            font-size: 0.52rem;
            color: #BFA898;
            font-family: 'Inter', sans-serif;
            text-transform: uppercase;
            letter-spacing: 0.5px;
            display: block;
        }
        .admin-lock-btn {
            background: transparent;
            border: 1px solid rgba(255,255,255,0.25);
            color: #FAF6F2;
            border-radius: 12px;
            padding: 5px 10px;
            font-size: 0.75rem;
            cursor: pointer;
            text-decoration: none;
        }
        .admin-banner {
            background: #D4A373;
            color: #1E1A17;
            padding: 8px 24px;
            font-size: 0.82rem;
            font-weight: 600;
            display: flex;
            justify-content: space-between;
            align-items: center;
        }
        .admin-banner a { color: #641E16; text-decoration: underline; cursor: pointer; }
        
        .container { 
            width: 100%;
            max-width: 1100px; 
            margin: 0 auto; 
            padding: 20px 16px; 
        }

        .category-grid {
            display: grid;
            grid-template-columns: repeat(2, 1fr);
            gap: 16px;
            margin: 20px 0;
        }
        .category-card {
            text-decoration: none;
            color: var(--text-dark);
            background: var(--card-foam);
            border: 2px solid var(--border-latte);
            border-radius: 20px;
            padding: 28px 20px;
            display: flex;
            flex-direction: column;
            align-items: center;
            justify-content: center;
            box-shadow: 0 4px 12px rgba(0,0,0,0.05);
            transition: transform 0.15s ease, box-shadow 0.15s ease, border-color 0.15s ease;
        }
        .category-card:hover {
            transform: translateY(-3px);
            box-shadow: 0 8px 20px rgba(0,0,0,0.1);
            border-color: var(--caramel);
        }
        .category-icon { font-size: 3rem; margin-bottom: 10px; }
        .category-title { font-family: 'Georgia', serif; font-size: 1.25rem; font-weight: 700; text-align: center; }

        .folder-grid {
            display: grid;
            grid-template-columns: repeat(auto-fill, minmax(180px, 1fr));
            gap: 14px;
            margin: 14px 0 24px 0;
        }
        .folder-card {
            text-decoration: none;
            color: var(--text-dark);
            background: var(--card-foam);
            border: 1px solid var(--border-latte);
            border-radius: 16px;
            padding: 20px 14px;
            display: flex;
            flex-direction: column;
            align-items: center;
            box-shadow: 0 2px 6px rgba(0,0,0,0.04);
            transition: transform 0.15s ease, box-shadow 0.15s ease;
        }
        .folder-card:hover {
            transform: translateY(-2px);
            box-shadow: 0 6px 14px rgba(0,0,0,0.08);
            border-color: var(--caramel);
        }
        .folder-card-icon { font-size: 2.4rem; margin-bottom: 8px; }
        .folder-card-title { font-size: 0.88rem; font-weight: 700; text-align: center; }
        .folder-card-count { font-size: 0.75rem; color: var(--text-muted); margin-top: 4px; }

        .folder-header-bar {
            display: flex;
            align-items: center;
            justify-content: space-between;
            background: var(--card-foam);
            border: 1px solid var(--border-latte);
            border-radius: 16px;
            padding: 16px 20px;
            margin-bottom: 16px;
        }
        .folder-header-title {
            display: flex;
            align-items: center;
            gap: 12px;
            font-family: 'Georgia', serif;
            font-size: 1.2rem;
            font-weight: bold;
        }
        .back-folder-btn {
            background: var(--espresso);
            color: #FAF6F2;
            text-decoration: none;
            padding: 8px 16px;
            border-radius: 12px;
            font-size: 0.82rem;
            font-weight: 600;
        }

        .search-box input {
            width: 100%; padding: 12px 20px; border-radius: 25px;
            border: 1px solid var(--border-latte); background: var(--card-foam);
            font-size: 0.95rem; outline: none; margin-bottom: 12px;
        }
        .filters { display: flex; gap: 10px; margin-bottom: 16px; }
        select {
            flex: 1; padding: 11px 14px; border-radius: 18px;
            border: 1px solid var(--border-latte); background: var(--card-foam);
            font-size: 0.88rem; outline: none;
        }

        .cards-layout-grid {
            display: grid;
            grid-template-columns: repeat(auto-fill, minmax(320px, 1fr));
            gap: 14px;
        }
        .card {
            background: var(--card-foam); border: 1px solid var(--border-latte);
            border-radius: 18px; padding: 18px; display: flex; flex-direction: column; justify-content: space-between;
        }
        .card-top { display: flex; justify-content: space-between; align-items: center; margin-bottom: 8px; }
        .badge { font-size: 0.78rem; font-weight: 700; color: var(--caramel); }
        .admin-actions { display: flex; gap: 8px; align-items: center; }
        .edit-btn { background: none; border: none; color: var(--caramel); font-size: 0.85rem; cursor: pointer; font-weight: 600; }
        .del-btn { background: none; border: none; color: #BA1A1A; font-size: 1.1rem; cursor: pointer; padding: 0 4px; }
        
        .card-title { font-family: 'Georgia', serif; font-size: 1.05rem; font-weight: bold; margin-bottom: 6px; }
        .card-meta { font-size: 0.82rem; color: var(--text-muted); margin-bottom: 14px; }
        .btn-pill {
            display: inline-block; text-align: center; width: 100%; padding: 10px 0;
            background: var(--espresso); color: #FAF6F2; text-decoration: none;
            font-size: 0.88rem; font-weight: 600; border-radius: 20px; border: none; cursor: pointer;
        }
        .btn-caramel { background: var(--caramel); }
        .fab-bar {
            position: fixed; bottom: 24px; left: 50%; transform: translateX(-50%);
            display: flex; gap: 12px; z-index: 100;
        }
        .fab {
            padding: 13px 22px; border-radius: 30px; background: var(--espresso);
            color: #FAF6F2; border: none; font-size: 0.92rem; font-weight: 600;
            box-shadow: 0 4px 16px rgba(0,0,0,0.25); cursor: pointer;
        }
        .modal {
            display: none; position: fixed; top: 0; left: 0; right: 0; bottom: 0;
            background: rgba(30,26,23,0.65); z-index: 200; align-items: center; justify-content: center;
        }
        .modal.active { display: flex; }
        .modal-content {
            background: var(--card-foam); width: 92%; max-width: 520px;
            border-radius: 20px; padding: 26px; max-height: 88vh; overflow-y: auto;
        }
        .modal-title { font-family: 'Georgia', serif; font-size: 1.25rem; font-weight: bold; margin-bottom: 14px; }
        .form-group { margin-bottom: 14px; }
        .form-group label { display: block; font-size: 0.82rem; font-weight: 600; margin-bottom: 5px; }
        .form-group input, .form-group select {
            width: 100%; padding: 11px; border-radius: 12px;
            border: 1px solid var(--border-latte); background: var(--bg-latte); outline: none;
        }
        .helper-text { font-size: 0.72rem; color: var(--text-muted); margin-top: 3px; }

        #pdfViewerModal {
            display: none;
            position: fixed;
            top: 0; left: 0; right: 0; bottom: 0;
            background: #14110E;
            z-index: 99999;
            flex-direction: column;
        }
        #pdfViewerModal.active { display: flex; }
        .pdf-viewer-header {
            background: var(--espresso);
            color: #FAF6F2;
            padding: 12px 20px;
            display: flex;
            align-items: center;
            justify-content: space-between;
            border-bottom: 1px solid rgba(255,255,255,0.1);
            gap: 12px;
        }
        .pdf-viewer-title {
            font-family: 'Georgia', serif;
            font-size: 0.95rem;
            font-weight: 600;
            white-space: nowrap;
            overflow: hidden;
            text-overflow: ellipsis;
            max-width: 55%;
        }
        .pdf-toolbar {
            display: flex;
            align-items: center;
            gap: 10px;
        }
        .zoom-controls {
            display: flex;
            background: rgba(255,255,255,0.08);
            border-radius: 14px;
            padding: 2px;
            border: 1px solid rgba(255,255,255,0.15);
        }
        .zoom-btn {
            background: transparent;
            color: #FAF6F2;
            border: none;
            padding: 6px 12px;
            border-radius: 10px;
            font-size: 0.9rem;
            font-weight: 700;
            cursor: pointer;
            transition: background 0.15s ease;
        }
        .zoom-btn:hover { background: rgba(255,255,255,0.15); }
        .zoom-btn:active { background: var(--caramel); }
        .zoom-level-text {
            color: #D8C7B6;
            font-size: 0.78rem;
            font-weight: 600;
            padding: 6px 8px;
            display: flex;
            align-items: center;
            min-width: 48px;
            justify-content: center;
        }
        .pdf-close-btn {
            background: var(--caramel);
            color: #FAF6F2;
            border: none;
            padding: 7px 15px;
            border-radius: 12px;
            font-size: 0.82rem;
            font-weight: 700;
            cursor: pointer;
        }
        #pdf-scroll-container {
            flex: 1;
            overflow: auto;
            background: #1E1A17;
            display: flex;
            flex-direction: column;
            align-items: center;
            padding: 20px 10px 50px 10px;
            -webkit-overflow-scrolling: touch;
            user-select: none;
            -webkit-user-select: none;
        }
        .pdf-canvas-wrap {
            margin: 0 auto 16px auto;
            box-shadow: 0 4px 20px rgba(0,0,0,0.6);
            border-radius: 4px;
            background: #FFFFFF;
            line-height: 0;
        }
        .pdf-page-canvas {
            display: block;
            border-radius: 4px;
            pointer-events: none;
        }
        .pdf-spinner {
            width: 44px; height: 44px;
            border: 3px solid rgba(167, 122, 83, 0.25);
            border-top: 3px solid var(--caramel);
            border-radius: 50%;
            animation: spinRing 0.9s linear infinite;
            margin-bottom: 14px;
        }
    </style>
</head>
<body>

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
            <div>Founded by <span class="founder-name">Shrinjay</span></div>
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

<div id="pdfViewerModal">
    <div class="pdf-viewer-header">
        <div class="pdf-viewer-title" id="pdfModalTitle">Viewing Document</div>
        <div class="pdf-toolbar">
            <div class="zoom-controls">
                <button class="zoom-btn" onclick="adjustZoom(-0.25)" title="Zoom Out">-</button>
                <div class="zoom-level-text" id="zoomLevelDisplay">100%</div>
                <button class="zoom-btn" onclick="adjustZoom(0.25)" title="Zoom In">+</button>
                <button class="zoom-btn" onclick="resetZoom()" title="Fit to Screen" style="border-left: 1px solid rgba(255,255,255,0.15);">⟲</button>
            </div>
            <button class="pdf-close-btn" onclick="closePdfViewer()">✕ Back</button>
        </div>
    </div>
    <div id="pdf-scroll-container" oncontextmenu="return false;"></div>
</div>

<div class="modal" id="uploadModal" onclick="if(event.target === this) closeModal('uploadModal')">
    <div class="modal-content">
        <div class="modal-title">Batch Upload PDFs</div>
        <form action="/upload" method="POST" enctype="multipart/form-data" onsubmit="showLoader()">
            <div class="form-group">
                <label>Resource Section</label>
                <select name="category">
                    <option value="pyq">📄 PYQs</option>
                    <option value="notes">📝 Notes</option>
                    <option value="practical">🔬 Practical</option>
                    <option value="timetable">📅 Timetable</option>
                </select>
            </div>
            <div class="form-group">
                <label>Course Categorization</label>
                <select name="course">
                    <option value="auto">⚡ Auto-Detect from Filename</option>
                    {upload_course_options}
                </select>
            </div>
            <div class="form-group">
                <label>Semester</label>
                <select name="sem">
                    <option value="auto">⚡ Auto-Detect from Filename</option>
                    {upload_sem_options}
                </select>
            </div>
            <div class="form-group">
                <label>Exam Year / Date</label>
                <input type="text" name="year" placeholder="e.g. 2024 or leave blank for Auto">
            </div>
            <div class="form-group">
                <label>Select All PDF Files</label>
                <input type="file" name="files" accept="application/pdf" multiple required>
            </div>
            <button type="submit" class="btn-pill" style="margin-top: 10px;">Upload Batch</button>
        </form>
    </div>
</div>

<div class="modal" id="linkModal" onclick="if(event.target === this) closeModal('linkModal')">
    <div class="modal-content">
        <div class="modal-title">Add Reference / External Link</div>
        <form action="/add-link" method="POST">
            <div class="form-group">
                <label>Resource Section</label>
                <select name="category">
                    <option value="timetable">📅 Timetable (External Link 🔗)</option>
                    <option value="pyq">📄 PYQs</option>
                    <option value="notes">📝 Notes</option>
                    <option value="practical">🔬 Practical</option>
                </select>
            </div>
            <div class="form-group"><label>Title</label><input type="text" name="title" placeholder="e.g. Official DU Timetable Link" required></div>
            <div class="form-group"><label>URL</label><input type="url" name="url" placeholder="https://..." required></div>
            <div class="form-group"><label>Course</label><select name="course" required>{upload_course_options}</select></div>
            <div class="form-group"><label>Semester</label><select name="sem" required>{upload_sem_options}</select></div>
            <button type="submit" class="btn-pill btn-caramel" style="margin-top: 8px;">Save External Link</button>
        </form>
    </div>
</div>

<div class="modal" id="editModal" onclick="if(event.target === this) closeModal('editModal')">
    <div class="modal-content">
        <div class="modal-title">Edit Resource Details</div>
        <form action="/edit-item" method="POST" onsubmit="showLoader()">
            <input type="hidden" name="item_id" id="edit_item_id">
            <div class="form-group">
                <label>Resource Section</label>
                <select name="category" id="edit_category">
                    <option value="pyq">📄 PYQs</option>
                    <option value="notes">📝 Notes</option>
                    <option value="practical">🔬 Practical</option>
                    <option value="timetable">📅 Timetable</option>
                </select>
            </div>
            <div class="form-group"><label>Title</label><input type="text" name="title" id="edit_title" required></div>
            <div class="form-group"><label>Course</label><select name="course" id="edit_course">{upload_course_options}</select></div>
            <div class="form-group"><label>Semester</label><select name="sem" id="edit_sem">{upload_sem_options}</select></div>
            <div class="form-group"><label>Year / Date</label><input type="text" name="year" id="edit_year" required></div>
            <button type="submit" class="btn-pill" style="margin-top: 8px;">Save Changes</button>
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
    if (window.pdfjsLib) {
        pdfjsLib.GlobalWorkerOptions.workerSrc = 'https://cdnjs.cloudflare.com/ajax/libs/pdf.js/3.11.174/pdf.worker.min.js';
    }

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

    window.addEventListener('load', () => { setTimeout(hideLoader, 200); });

    document.addEventListener('DOMContentLoaded', () => {
        const triggers = document.querySelectorAll('a.folder-card, a.category-card, a.back-folder-btn');
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
            const loadingTask = pdfjsLib.getDocument(url);
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

    async function renderPdfPages() {
        if (!activePdfDoc) return;
        const container = document.getElementById('pdf-scroll-container');
        container.innerHTML = '';

        const dpr = Math.min(window.devicePixelRatio || 1, 2.5);
        const availableWidth = Math.min(window.innerWidth - 32, 950);

        for (let pageNum = 1; pageNum <= activePdfDoc.numPages; pageNum++) {
            const page = await activePdfDoc.getPage(pageNum);
            
            const unscaledViewport = page.getViewport({ scale: 1.0 });
            const baseScale = availableWidth / unscaledViewport.width;
            const finalScale = baseScale * currentZoomMultiplier;

            const cssViewport = page.getViewport({ scale: finalScale });
            const highResViewport = page.getViewport({ scale: finalScale * dpr });

            const canvasWrap = document.createElement('div');
            canvasWrap.className = 'pdf-canvas-wrap';

            const canvas = document.createElement('canvas');
            canvas.className = 'pdf-page-canvas';
            canvas.height = highResViewport.height;
            canvas.width = highResViewport.width;
            canvas.style.width = cssViewport.width + 'px';
            canvas.style.height = cssViewport.height + 'px';

            canvasWrap.appendChild(canvas);
            container.appendChild(canvasWrap);

            const context = canvas.getContext('2d');
            await page.render({
                canvasContext: context,
                viewport: highResViewport
            }).promise;
        }
    }

    function adjustZoom(delta) {
        currentZoomMultiplier = Math.max(0.5, Math.min(3.0, currentZoomMultiplier + delta));
        updateZoomDisplay();
        renderPdfPages();
    }

    function resetZoom() {
        currentZoomMultiplier = 1.0;
        updateZoomDisplay();
        renderPdfPages();
    }

    function updateZoomDisplay() {
        const el = document.getElementById('zoomLevelDisplay');
        if (el) el.innerText = Math.round(currentZoomMultiplier * 100) + '%';
    }

    function closePdfViewer() {
        document.getElementById('pdfViewerModal').classList.remove('active');
        activePdfDoc = null;
        if (history.state && history.state.pdfOpen) {
            history.back();
        }
    }

    window.addEventListener('popstate', (e) => {
        if (document.getElementById('pdfViewerModal').classList.contains('active')) {
            closePdfViewer();
        }
    });

    function openEditModal(id, title, course, sem, year, category) {
        document.getElementById('edit_item_id').value = id;
        document.getElementById('edit_title').value = title;
        document.getElementById('edit_course').value = course;
        document.getElementById('edit_sem').value = sem;
        document.getElementById('edit_year').value = year;
        document.getElementById('edit_category').value = category || 'pyq';
        openModal('editModal');
    }

    function filterCards() {
        const searchVal = document.getElementById('searchInput').value.toLowerCase();
        const semVal = document.getElementById('semFilter').value;
        const cards = document.querySelectorAll('.card');

        cards.forEach(card => {
            const title = card.getAttribute('data-title').toLowerCase();
            const course = card.getAttribute('data-course').toLowerCase();
            const sem = card.getAttribute('data-sem');

            const matchesSearch = title.includes(searchVal) || course.includes(searchVal);
            const matchesSem = (semVal === 'All Semesters' || sem === semVal);

            card.style.display = (matchesSearch && matchesSem) ? 'flex' : 'none';
        });
    }

    if ('serviceWorker' in navigator) {
        window.addEventListener('load', () => {
            navigator.serviceWorker.register('/sw.js');
        });
    }
</script>

</body>
</html>
"""

def is_admin(request: Request) -> bool:
    return request.cookies.get("vault_admin") == "1"

def render_page(request: Request, main_content: str) -> HTMLResponse:
    admin_active = is_admin(request)
    
    admin_header_btn = '<a class="admin-lock-btn" onclick="openModal(\'loginModal\')">🔒 Admin Access</a>'
    if admin_active:
        admin_header_btn = '<a class="admin-lock-btn" href="/logout">🔓 Exit Admin Mode</a>'

    admin_banner_html = ""
    if admin_active:
        admin_banner_html = """
        <div class="admin-banner">
            <span>⚡ Host Admin Session Active — You have full management privileges.</span>
            <a href="/logout">Logout Admin</a>
        </div>
        """

    fab_controls = ""
    if admin_active:
        fab_controls = """
        <div class="fab-bar">
            <button class="fab" onclick="openModal('uploadModal')">📤 Batch Upload PDFs</button>
            <button class="fab" onclick="openModal('linkModal')" style="background: var(--caramel);">🔗 Add Link</button>
        </div>
        """

    course_opts = "".join([f'<option value="{c}">{c}</option>' for c in COURSES if c != "All Courses"])
    sem_opts = "".join([f'<option value="{s}">{s}</option>' for s in SEMESTERS if s != "All Semesters"])

    html = HTML_TEMPLATE.format(
        admin_header_btn=admin_header_btn,
        admin_banner_html=admin_banner_html,
        main_view_content=main_content,
        fab_controls=fab_controls,
        upload_course_options=course_opts,
        upload_sem_options=sem_opts
    )
    return HTMLResponse(content=html)

def auto_detect_metadata(filename: str):
    fname = filename.upper()
    
    sem = "Sem 1"
    sem_match = re.search(r'SEM(?:ESTER)?[\s\-_]*([1-6])', fname)
    if sem_match:
        sem = f"Sem {sem_match.group(1)}"
    
    year = "2024"
    year_match = re.search(r'(20\d{2})', fname)
    if year_match:
        year = year_match.group(1)

    course = "General / Other"
    if "BOTANY" in fname:
        course = "B.Sc (Hons) Botany"
    elif "ZOOLOGY" in fname:
        course = "B.Sc (Hons) Zoology"
    elif "LIFE SCIENCE" in fname or "LIFE_SCIENCE" in fname:
        course = "B.Sc (prog) Life Sciences"
    elif "CHEMISTRY" in fname or "CHEM" in fname:
        course = "B.Sc (Hons) Chemistry"
    elif "MATH" in fname:
        course = "B.Sc (Hons) Mathematics"
    elif "PHYSICS" in fname or "PHYS" in fname:
        course = "B.Sc (Hons) Physics"
    elif "COMPUTER" in fname or "CS" in fname:
        course = "B.Sc (Hons) Computer Science"
    elif "BCOM" in fname or "B.COM" in fname:
        course = "B.Com (Hons)"
    elif "ECONOMICS" in fname or "ECO" in fname:
        course = "B.A. (Hons) Economics"
    elif "POL" in fname or "POLITICAL" in fname:
        course = "B.A. (Hons) Political Science"
    elif "ENGLISH" in fname:
        course = "B.A. (Hons) English"

    title = re.sub(r'\.pdf$', '', filename, flags=re.IGNORECASE)
    title = re.sub(r'[\-_]', ' ', title).strip()

    return title, course, sem, year

@app.get("/", response_class=HTMLResponse)
def index(request: Request):
    conn = get_db()
    cursor = conn.cursor()
    
    counts = {}
    for cat in CATEGORY_MAP.keys():
        cursor.execute("SELECT COUNT(*) FROM du_resources WHERE category = ?", (cat,))
        counts[cat] = cursor.fetchone()[0]
    conn.close()

    cards_html = ""
    for cat_key, meta in CATEGORY_MAP.items():
        cards_html += f"""
        <a href="/category/{cat_key}" class="category-card">
            <div class="category-icon">{meta['icon']}</div>
            <div class="category-title">{meta['name']}</div>
            <div class="folder-card-count" style="margin-top:6px;">{counts.get(cat_key, 0)} Items Available</div>
        </a>
        """

    content = f"""
    <div style="text-align: center; margin-bottom: 24px;">
        <h1 style="font-family: 'Georgia', serif; font-size: 1.8rem; margin-bottom: 6px;">Delhi University Vault</h1>
        <p style="color: var(--text-muted); font-size: 0.95rem;">Select a category to browse past papers, study material, and course resources.</p>
    </div>
    <div class="category-grid">
        {cards_html}
    </div>
    """
    return render_page(request, content)

@app.get("/category/{cat}", response_class=HTMLResponse)
def view_category(cat: str, request: Request):
    if cat not in CATEGORY_MAP:
        return RedirectResponse(url="/", status_code=303)

    cat_meta = CATEGORY_MAP[cat]
    conn = get_db()
    cursor = conn.cursor()

    cursor.execute("SELECT course, COUNT(*) FROM du_resources WHERE category = ? GROUP BY course", (cat,))
    course_counts = dict(cursor.fetchall())
    conn.close()

    folders_html = ""
    for tile in COURSE_TILES:
        c_name = tile["name"]
        count = course_counts.get(c_name, 0)
        encoded_course = urllib.parse.quote(c_name)
        folders_html += f"""
        <a href="/folder/{cat}/{encoded_course}" class="folder-card">
            <div class="folder-card-icon">{tile['icon']}</div>
            <div class="folder-card-title">{tile['label']}</div>
            <div class="folder-card-count">{count} items</div>
        </a>
        """

    content = f"""
    <div class="folder-header-bar">
        <div class="folder-header-title">
            <span>{cat_meta['icon']}</span>
            <span>{cat_meta['name']}</span>
        </div>
        <a href="/" class="back-folder-btn">← Back Home</a>
    </div>
    <div class="folder-grid">
        {folders_html}
    </div>
    """
    return render_page(request, content)

@app.get("/folder/{cat}/{course_path}", response_class=HTMLResponse)
def view_folder(cat: str, course_path: str, request: Request):
    course = urllib.parse.unquote(course_path)
    conn = get_db()
    cursor = conn.cursor()
    
    cursor.execute("""
        SELECT id, title, course, semester, year, type, category, url_or_name, file_size
        FROM du_resources 
        WHERE category = ? AND course = ?
        ORDER BY year DESC, title ASC
    """, (cat, course))
    
    items = cursor.fetchall()
    conn.close()

    admin_active = is_admin(request)
    cards_html = ""

    for item in items:
        item_id, title, c_course, sem, year, r_type, c_cat, url_or_name, file_size = item
        
        if r_type == "link":
            action_btn = f'<a href="{url_or_name}" target="_blank" class="btn-pill btn-caramel">🔗 Open External Link</a>'
        else:
            action_btn = f'<button onclick="openPdfViewer(\'/view/{item_id}\', \'{title.replace("\'", "\\\'")}\')" class="btn-pill">📄 View Document</button>'

        admin_tools = ""
        if admin_active:
            admin_tools = f"""
            <div class="admin-actions">
                <button class="edit-btn" onclick="openEditModal({item_id}, '{title.replace("\'", "\\\'")}', '{c_course.replace("\'", "\\\'")}', '{sem}', '{year}', '{c_cat}')">✏️ Edit</button>
                <form action="/delete/{item_id}" method="POST" style="display:inline;" onsubmit="return confirm('Delete this resource?')">
                    <button type="submit" class="del-btn" title="Delete Resource">🗑️</button>
                </form>
            </div>
            """

        cards_html += f"""
        <div class="card" data-title="{title}" data-course="{c_course}" data-sem="{sem}">
            <div>
                <div class="card-top">
                    <span class="badge">{sem} • {year or 'N/A'}</span>
                    {admin_tools}
                </div>
                <div class="card-title">{title}</div>
                <div class="card-meta">{c_course} {f"• {file_size}" if file_size else ""}</div>
            </div>
            <div style="margin-top: 12px;">
                {action_btn}
            </div>
        </div>
        """

    if not items:
        cards_html = """
        <div style="grid-column: 1 / -1; text-align: center; padding: 40px 20px; color: var(--text-muted);">
            <p>No resources found in this folder yet.</p>
        </div>
        """

    sem_options = "".join([f'<option value="{s}">{s}</option>' for s in SEMESTERS])

    content = f"""
    <div class="folder-header-bar">
        <div class="folder-header-title">
            <span>📚</span>
            <span>{course}</span>
        </div>
        <a href="/category/{cat}" class="back-folder-btn">← Back to Category</a>
    </div>

    <div class="search-box">
        <input type="text" id="searchInput" placeholder="🔍 Search papers or topics..." onkeyup="filterCards()">
    </div>

    <div class="filters">
        <select id="semFilter" onchange="filterCards()">
            {sem_options}
        </select>
    </div>

    <div class="cards-layout-grid">
        {cards_html}
    </div>
    """
    return render_page(request, content)

@app.post("/upload")
async def handle_upload(
    request: Request,
    category: str = Form("pyq"),
    course: str = Form("auto"),
    sem: str = Form("auto"),
    year: str = Form(""),
    files: list[UploadFile] = File(...)
):
    if not is_admin(request):
        return RedirectResponse(url="/", status_code=303)

    conn = get_db()
    cursor = conn.cursor()

    for file in files:
        if not file.filename:
            continue
        
        contents = await file.read()
        size_kb = round(len(contents) / 1024, 1)
        file_size_str = f"{size_kb} KB" if size_kb < 1024 else f"{round(size_kb/1024, 2)} MB"

        auto_title, auto_course, auto_sem, auto_year = auto_detect_metadata(file.filename)

        final_course = auto_course if course == "auto" else course
        final_sem = auto_sem if sem == "auto" else sem
        final_year = year.strip() if year.strip() else auto_year

        cursor.execute("""
            INSERT INTO du_resources (title, course, semester, year, type, category, url_or_name, file_data, file_size)
            VALUES (?, ?, ?, ?, 'pdf', ?, ?, ?, ?)
        """, (auto_title, final_course, final_sem, final_year, category, file.filename, contents, file_size_str))

    conn.commit()
    conn.close()
    return RedirectResponse(url=f"/category/{category}", status_code=303)

@app.post("/add-link")
def add_link(
    request: Request,
    category: str = Form("timetable"),
    title: str = Form(...),
    url: str = Form(...),
    course: str = Form(...),
    sem: str = Form(...)
):
    if not is_admin(request):
        return RedirectResponse(url="/", status_code=303)

    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("""
        INSERT INTO du_resources (title, course, semester, year, type, category, url_or_name)
        VALUES (?, ?, ?, '2024', 'link', ?, ?)
    """, (title, course, sem, category, url))
    
    conn.commit()
    conn.close()
    return RedirectResponse(url=f"/category/{category}", status_code=303)

@app.post("/edit-item")
def edit_item(
    request: Request,
    item_id: int = Form(...),
    title: str = Form(...),
    course: str = Form(...),
    sem: str = Form(...),
    year: str = Form(...),
    category: str = Form("pyq")
):
    if not is_admin(request):
        return RedirectResponse(url="/", status_code=303)

    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("""
        UPDATE du_resources
        SET title = ?, course = ?, semester = ?, year = ?, category = ?
        WHERE id = ?
    """, (title, course, sem, year, category, item_id))
    
    conn.commit()
    conn.close()
    return RedirectResponse(url=f"/category/{category}", status_code=303)

@app.post("/delete/{item_id}")
def delete_item(item_id: int, request: Request):
    if not is_admin(request):
        return RedirectResponse(url="/", status_code=303)

    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("SELECT category FROM du_resources WHERE id = ?", (item_id,))
    res = cursor.fetchone()
    cat = res[0] if res else "pyq"

    cursor.execute("DELETE FROM du_resources WHERE id = ?", (item_id,))
    conn.commit()
    conn.close()
    return RedirectResponse(url=f"/category/{cat}", status_code=303)

@app.get("/view/{item_id}")
def view_pdf(item_id: int):
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("SELECT file_data, url_or_name FROM du_resources WHERE id = ?", (item_id,))
    res = cursor.fetchone()
    conn.close()

    if not res or not res[0]:
        return Response(content="File Not Found", status_code=404)

    file_bytes, filename = res
    headers = {
        "Content-Disposition": f"inline; filename=\"{filename or 'document.pdf'}\"",
        "Cache-Control": "public, max-age=31536000, immutable"
    }
    return StreamingResponse(io.BytesIO(file_bytes), media_type="application/pdf", headers=headers)

@app.post("/login")
def login(password: str = Form(...)):
    response = RedirectResponse(url="/", status_code=303)
    if password == ADMIN_PASSWORD:
        response.set_cookie(key="vault_admin", value="1", max_age=86400 * 30, httponly=True)
    return response

@app.get("/logout")
def logout():
    response = RedirectResponse(url="/", status_code=303)
    response.delete_cookie(key="vault_admin")
    return response
