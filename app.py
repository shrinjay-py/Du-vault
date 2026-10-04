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
    # Ensure column 'category' exists for legacy DBs
    try:
        cursor.execute("ALTER TABLE du_resources ADD COLUMN category TEXT DEFAULT 'pyq'")
    except Exception:
        pass
    conn.commit()
    conn.close()

init_db()

# --- PWA Manifest & Assets ---
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

        /* Loading Screen */
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

        /* Top Category Grid */
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

        /* Fullscreen In-App PDF Viewer */
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

<!-- Native In-App PDF Viewing Stage -->
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

<!-- BATCH UPLOAD MODAL -->
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

<!-- LINK MODAL -->
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

<!-- EDIT ITEM MODAL -->
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

<!-- LOGIN MODAL -->
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

            const wrap = document.createElement('div');
            wrap.className = 'pdf-canvas-wrap';

            const canvas = document.createElement('canvas');
            canvas.className = 'pdf-page-canvas';
            const context = canvas.getContext('2d');

            canvas.width = Math.floor(highResViewport.width);
            canvas.height = Math.floor(highResViewport.height);

            canvas.style.width = Math.floor(cssViewport.width) + 'px';
            canvas.style.height = Math.floor(cssViewport.height) + 'px';

            wrap.appendChild(canvas);
            container.appendChild(wrap);

            await page.render({
                canvasContext: context,
                viewport: highResViewport
            }).promise;
        }
    }

    function updateZoomDisplay() {
        const display = document.getElementById('zoomLevelDisplay');
        if (display) {
            display.innerText = Math.round(currentZoomMultiplier * 100) + '%';
        }
    }

    function adjustZoom(delta) {
        const nextZoom = currentZoomMultiplier + delta;
        if (nextZoom >= 0.5 && nextZoom <= 3.0) {
            currentZoomMultiplier = Math.round(nextZoom * 100) / 100;
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
            if (!isPopState && history.state && history.state.pdfOpen) {
                history.back();
            }
        }
    }

    window.addEventListener('popstate', (e) => {
        const viewer = document.getElementById('pdfViewerModal');
        if (viewer && viewer.classList.contains('active')) {
            closePdfViewer(true);
        }
    });

    function openEditModal(id, title, course, sem, year, category) {
        document.getElementById('edit_item_id').value = id;
        document.getElementById('edit_title').value = title;
        document.getElementById('edit_course').value = course;
        document.getElementById('edit_sem').value = sem;
        document.getElementById('edit_year').value = year;
        if (document.getElementById('edit_category')) {
            document.getElementById('edit_category').value = category || 'pyq';
        }
        openModal('editModal');
    }

    function filterCardsLive(query) {
        const term = query.toLowerCase().trim();
        const cards = document.querySelectorAll('.card-item');
        cards.forEach(card => {
            const text = card.getAttribute('data-search-text') || '';
            if (text.includes(term)) {
                card.style.display = 'flex';
            } else {
                card.style.display = 'none';
            }
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

def parse_filename(filename: str, fallback_course: str, fallback_sem: str, fallback_year: str):
    lower = filename.lower()
    
    detected_year = fallback_year.strip() if fallback_year.strip() else None
    if not detected_year:
        year_match = re.search(r'\b(20[1-2][0-9])\b', lower)
        detected_year = year_match.group(1) if year_match else "2024"

    detected_sem = fallback_sem
    if fallback_sem == "auto":
        sem_match = re.search(r'(?:sem(?:ester)?[\s_-]*([1-6])|\bs([1-6])\b)', lower)
        if sem_match:
            digit = sem_match.group(1) or sem_match.group(2)
            detected_sem = f"Sem {digit}"
        else:
            detected_sem = "Sem 1"

    detected_course = fallback_course
    if fallback_course == "auto":
        botany_keywords = [
            "botany", "plant", "archegoniate", "bryophyte", "pteridophyte", 
            "gymnosperm", "angiosperm", "algae", "microbiology", "mycology", 
            "phytopathology", "plant physiology", "plant metabolism", "plant ecology"
        ]
        zoology_keywords = [
            "zoology", "animal", "chordata", "non-chordata", "physiology", 
            "developmental biology", "genetics", "evolution"
        ]
        
        if any(k in lower for k in botany_keywords):
            detected_course = "B.Sc (Hons) Botany"
        elif any(k in lower for k in zoology_keywords):
            detected_course = "B.Sc (Hons) Zoology"
        elif "life science" in lower or "life-science" in lower:
            detected_course = "B.Sc (prog) Life Sciences"
        elif any(k in lower for k in ["cs", "computer", "c++", "python", "algorithm", "data structure", "dbms", "os"]):
            detected_course = "B.Sc (Hons) Computer Science"
        elif any(k in lower for k in ["math", "calculus", "algebra", "differential", "real analysis"]):
            detected_course = "B.Sc (Hons) Mathematics"
        elif any(k in lower for k in ["physic", "mechanics", "optics", "electromagnet", "quantum"]):
            detected_course = "B.Sc (Hons) Physics"
        elif any(k in lower for k in ["physical science", "physical science with chemistry"]):
            detected_course = "B.Sc (prog) Physical science with Chemistry"
        elif any(k in lower for k in ["chemistry", "organic", "inorganic", "physical chem"]):
            detected_course = "B.Sc (Hons) Chemistry"
        elif "bcom hons" in lower or "b.com (h)" in lower:
            detected_course = "B.Com (Hons)"
        elif "bcom" in lower or "b.com" in lower:
            detected_course = "B.Com (Programme)"
        elif "econ" in lower or "macro" in lower or "micro" in lower:
            detected_course = "B.A. (Hons) Economics"
        elif "english" in lower or "literature" in lower:
            detected_course = "B.A. (Hons) English"
        elif any(k in lower for k in ["pol", "constitution", "governance", "political science"]):
            detected_course = "B.A. (Hons) Political Science"
        elif "ba prog" in lower or "b.a prog" in lower:
            detected_course = "B.A. Programme"
        elif "cic" in lower or "b.tech" in lower:
            detected_course = "B.Tech / CIC"
        else:
            detected_course = "General / Other"

    base = os.path.splitext(filename)[0]
    clean_title = re.sub(r'_+', ' ', base).strip().title()
    return clean_title, detected_course, detected_sem, detected_year

@app.get("/", response_class=HTMLResponse)
def index(request: Request, cat: str = "", course: str = "", sem: str = "All Semesters", q: str = ""):
    is_admin = request.cookies.get("du_admin_session") == "authenticated"
    conn = get_db()
    cursor = conn.cursor()

    main_view_content = ""

    # LEVEL 1: Main display with PYQs, Notes, Practical, Timetable icons
    if not cat and not course and not q.strip():
        main_view_content = """
        <div style="text-align: center; margin: 10px 0 20px 0;">
            <h2 style="font-family: 'Georgia', serif; font-size: 1.6rem; color: var(--espresso);">Select Resource Vault</h2>
            <p style="color: var(--text-muted); font-size: 0.88rem; margin-top: 4px;">Choose a section to view past papers, notes, or timetables</p>
        </div>
        <div class="category-grid">
            <a href="/?cat=pyq" class="category-card">
                <div class="category-icon">📄</div>
                <div class="category-title">PYQs</div>
            </a>
            <a href="/?cat=notes" class="category-card">
                <div class="category-icon">📝</div>
                <div class="category-title">Notes</div>
            </a>
            <a href="/?cat=practical" class="category-card">
                <div class="category-icon">🔬</div>
                <div class="category-title">Practical</div>
            </a>
            <a href="/?cat=timetable" class="category-card">
                <div class="category-icon">📅 🔗</div>
                <div class="category-title">Timetable</div>
            </a>
        </div>
        <form method="GET" action="/" style="margin-top: 20px;">
            <div class="search-box">
                <input type="text" name="q" placeholder="🔍 Search any paper, notes, or resources...">
            </div>
        </form>
        """

    # LEVEL 2: Inside a section (e.g. PYQs), show all subject/course tiles
    elif cat and not course and not q.strip():
        category_labels = {
            "pyq": "📄 PYQs Vault",
            "notes": "📝 Notes Vault",
            "practical": "🔬 Practical Vault",
            "timetable": "📅 Timetables (External Links 🔗)"
        }
        current_cat_label = category_labels.get(cat, "Vault")

        if cat == "timetable":
            # Direct listing for Timetable external links
            cursor.execute("SELECT id, title, course, semester, year, type, url_or_name, file_size FROM du_resources WHERE category = 'timetable' ORDER BY id DESC")
            records = cursor.fetchall()
            
            header_bar = f"""
            <div class="folder-header-bar">
                <div class="folder-header-title">
                    <span>📅</span>
                    <span>Timetable & External Links</span>
                </div>
                <a href="/" class="back-folder-btn">← Main Menu</a>
            </div>
            """
            
            cards_html = ""
            if not records:
                cards_html = '<div class="card" style="text-align:center; padding:40px;"><p style="color:var(--text-muted);">No timetable links added yet.</p></div>'
            else:
                cards_html = '<div class="cards-layout-grid">'
                for item_id, title, c, s, y, r_type, url_or_name, size in records:
                    admin_opts = ""
                    if is_admin:
                        safe_title = title.replace("'", "\\'")
                        safe_c = c.replace("'", "\\'")
                        admin_opts = f"""
                        <div class="admin-actions">
                            <button class="edit-btn" onclick="openEditModal({item_id}, '{safe_title}', '{safe_c}', '{s}', '{y}', 'timetable')">✎ Edit</button>
                            <form action="/delete/{item_id}" method="POST" onsubmit="return confirm('Delete item?');">
                                <button type="submit" class="del-btn">✕</button>
                            </form>
                        </div>
                        """
                    cards_html += f"""
                    <div class="card card-item">
                        <div>
                            <div class="card-top">
                                <span class="badge">{c}</span>
                                {admin_opts}
                            </div>
                            <div class="card-title">{title}</div>
                            <div class="card-meta">🎓 {s} &nbsp;•&nbsp; 📅 {y}</div>
                        </div>
                        <div style="margin-top: 10px;">
                            <a class="btn-pill btn-caramel" href="{url_or_name}" target="_blank">🔗 Open External Link</a>
                        </div>
                    </div>
                    """
                cards_html += '</div>'
            main_view_content = header_bar + cards_html
        else:
            cursor.execute("SELECT course, COUNT(*) FROM du_resources WHERE category = ? GROUP BY course", (cat,))
            counts = dict(cursor.fetchall())

            header_bar = f"""
            <div class="folder-header-bar">
                <div class="folder-header-title">
                    <span>{current_cat_label}</span>
                </div>
                <a href="/" class="back-folder-btn">← Main Menu</a>
            </div>
            """

            folders_grid = '<div style="font-size: 0.85rem; font-weight: 700; text-transform: uppercase; color: var(--text-muted); margin-bottom: 8px;">Select Course / Subject Folder</div>'
            folders_grid += '<div class="folder-grid">'
            for tile in COURSE_TILES:
                encoded_c = urllib.parse.quote_plus(tile["name"])
                num_papers = counts.get(tile["name"], 0)
                folders_grid += f"""
                <a href="/?cat={cat}&course={encoded_c}" class="folder-card">
                    <div class="folder-card-icon">{tile["icon"]}</div>
                    <div class="folder-card-title">{tile["label"]}</div>
                    <div class="folder-card-count">{num_papers} items</div>
                </a>
                """
            folders_grid += '</div>'

            search_bar = f"""
            <form method="GET" action="/">
                <input type="hidden" name="cat" value="{cat}">
                <div class="search-box" style="margin-top: 10px;">
                    <input type="text" name="q" placeholder="🔍 Search inside {current_cat_label}...">
                </div>
            </form>
            """
            main_view_content = header_bar + search_bar + folders_grid

    # LEVEL 3: Inside a course icon (e.g. Botany inside PYQs) -> PDFs with Filters
    else:
        query = "SELECT id, title, course, semester, year, type, url_or_name, file_size, category FROM du_resources WHERE 1=1"
        params = []

        if cat:
            query += " AND category = ?"
            params.append(cat)
        if course:
            query += " AND course = ?"
            params.append(course)
        if sem != "All Semesters":
            query += " AND semester = ?"
            params.append(sem)
        if q.strip():
            query += " AND (LOWER(title) LIKE ? OR LOWER(course) LIKE ? OR LOWER(year) LIKE ?)"
            wc = f"%{q.strip().lower()}%"
            params.extend([wc, wc, wc])

        query += " ORDER BY id DESC"
        cursor.execute(query, tuple(params))
        records = cursor.fetchall()

        matched_tile = next((t for t in COURSE_TILES if t["name"] == course), None)
        folder_icon = matched_tile["icon"] if matched_tile else "📁"
        folder_display_name = matched_tile["label"] if matched_tile else (course or f"Search: '{q}'")
        
        back_link = f"/?cat={cat}" if cat else "/"

        header_bar = f"""
        <div class="folder-header-bar">
            <div class="folder-header-title">
                <span>{folder_icon}</span>
                <span>{folder_display_name}</span>
            </div>
            <a href="{back_link}" class="back-folder-btn">← Back to Folders</a>
        </div>
        """

        search_filter_form = f"""
        <form method="GET" action="/">
            <input type="hidden" name="cat" value="{cat}">
            <input type="hidden" name="course" value="{course}">
            <div class="search-box">
                <input type="text" name="q" value="{q}" placeholder="⚡ Live search by subject, title or date..." onkeyup="filterCardsLive(this.value)">
            </div>
            <div class="filters">
                <select name="sem" onchange="this.form.submit()">{ "".join(f'<option value="{s}" {"selected" if s == sem else ""}>{s}</option>' for s in SEMESTERS) }</select>
            </div>
        </form>
        """

        cards_html = ""
        if not records:
            cards_html = f'<div class="card" style="text-align:center; padding:40px;"><p style="color:var(--text-muted);">No documents found in this folder.</p></div>'
        else:
            cards_html = '<div class="cards-layout-grid">'
            for item_id, title, c, s, y, r_type, url_or_name, size, item_cat in records:
                safe_title_view = title.replace("'", "\\'")
                if r_type == "pdf":
                    action_btn = f"""
                    <div style="margin-top: 10px;">
                        <button class="btn-pill" onclick="openPdfViewer('/view/{item_id}', '{safe_title_view}')">View PDF</button>
                    </div>
                    """
                else:
                    action_btn = f'<div style="margin-top: 10px;"><a class="btn-pill btn-caramel" href="{url_or_name}" target="_blank">🔗 Open Link</a></div>'

                admin_opts = ""
                if is_admin:
                    safe_title = title.replace("'", "\\'")
                    safe_c = c.replace("'", "\\'")
                    admin_opts = f"""
                    <div class="admin-actions">
                        <button class="edit-btn" onclick="openEditModal({item_id}, '{safe_title}', '{safe_c}', '{s}', '{y}', '{item_cat}')">✎ Edit</button>
                        <form action="/delete/{item_id}" method="POST" onsubmit="return confirm('Delete paper?');">
                            <button type="submit" class="del-btn">✕</button>
                        </form>
                    </div>
                    """

                card_search_data = f"{title.lower()} {c.lower()} {s.lower()} {y.lower()}"

                cards_html += f"""
                <div class="card card-item" data-search-text="{card_search_data}">
                    <div>
                        <div class="card-top">
                            <span class="badge">{c}</span>
                            {admin_opts}
                        </div>
                        <div class="card-title">{title}</div>
                        <div class="card-meta">🎓 {s} &nbsp;•&nbsp; 📅 Filter/Date: {y} &nbsp;•&nbsp; 💾 {size}</div>
                    </div>
                    {action_btn}
                </div>
                """
            cards_html += '</div>'

        main_view_content = header_bar + search_filter_form + f'<div id="cards-container">{cards_html}</div>'

    conn.close()

    up_course_opts = "".join(f'<option value="{c}">{c}</option>' for c in COURSES[1:])
    up_sem_opts = "".join(f'<option value="{s}">{s}</option>' for s in SEMESTERS[1:])

    if is_admin:
        admin_header_btn = '<a href="/logout" class="admin-lock-btn">Logout</a>'
        admin_banner_html = '<div class="admin-banner"><span>🔓 Host Controls Unlocked</span><a href="/logout">Lock</a></div>'
        fab_controls = """
        <div class="fab-bar">
            <button class="fab" onclick="openModal('uploadModal')">📁 Batch Upload</button>
            <button class="fab" style="background: var(--caramel);" onclick="openModal('linkModal')">🔗 Add Link</button>
        </div>
        """
    else:
        admin_header_btn = '<button onclick="openModal(\'loginModal\')" class="admin-lock-btn">🔒 Admin</button>'
        admin_banner_html = ""
        fab_controls = ""

    content = HTML_TEMPLATE
    content = content.replace("{main_view_content}", main_view_content)
    content = content.replace("{upload_course_options}", up_course_opts)
    content = content.replace("{upload_sem_options}", up_sem_opts)
    content = content.replace("{admin_header_btn}", admin_header_btn)
    content = content.replace("{admin_banner_html}", admin_banner_html)
    content = content.replace("{fab_controls}", fab_controls)

    return HTMLResponse(content=content)

@app.post("/login")
def login(password: str = Form(...)):
    if password == ADMIN_PASSWORD:
        response = RedirectResponse(url="/", status_code=303)
        response.set_cookie(key="du_admin_session", value="authenticated", httponly=True)
        return response
    return HTMLResponse("Invalid Password", status_code=401)

@app.get("/logout")
def logout():
    response = RedirectResponse(url="/", status_code=303)
    response.delete_cookie(key="du_admin_session")
    return response

@app.post("/upload")
async def upload_files(
    request: Request,
    category: str = Form("pyq"),
    course: str = Form("auto"),
    sem: str = Form("auto"),
    year: str = Form(""),
    files: list[UploadFile] = File(...)
):
    if request.cookies.get("du_admin_session") != "authenticated":
        return HTMLResponse("Unauthorized. Please log in as Admin.", status_code=403)

    conn = get_db()
    cursor = conn.cursor()
    last_detected_course = None
    for file in files:
        if file.filename.lower().endswith(".pdf"):
            data = await file.read()
            size_mb = f"{len(data) / (1024 * 1024):.2f} MB"
            clean_title, detected_course, detected_sem, detected_year = parse_filename(
                file.filename, course, sem, year
            )
            last_detected_course = detected_course
            cursor.execute("""
                INSERT INTO du_resources (title, course, semester, year, type, category, url_or_name, file_data, file_size)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (clean_title, detected_course, detected_sem, detected_year, "pdf", category, file.filename, data, size_mb))
    conn.commit()
    conn.close()

    redirect_url = f"/?cat={category}&course={urllib.parse.quote_plus(last_detected_course)}" if last_detected_course else f"/?cat={category}"
    return RedirectResponse(url=redirect_url, status_code=303)

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
    if request.cookies.get("du_admin_session") != "authenticated":
        return HTMLResponse("Unauthorized.", status_code=403)

    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("""
        UPDATE du_resources
        SET title = ?, course = ?, semester = ?, year = ?, category = ?
        WHERE id = ?
    """, (title, course, sem, year, category, item_id))
    conn.commit()
    conn.close()

    return RedirectResponse(url=f"/?cat={category}&course={urllib.parse.quote_plus(course)}", status_code=303)

@app.post("/add-link")
def add_link(
    request: Request,
    title: str = Form(...),
    url: str = Form(...),
    course: str = Form(...),
    sem: str = Form(...),
    category: str = Form("timetable")
):
    if request.cookies.get("du_admin_session") != "authenticated":
        return HTMLResponse("Unauthorized.", status_code=403)

    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("""
        INSERT INTO du_resources (title, course, semester, year, type, category, url_or_name, file_data, file_size)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (title, course, sem, "External", "link", category, url, None, "Link"))
    conn.commit()
    conn.close()
    return RedirectResponse(url=f"/?cat={category}", status_code=303)

@app.get("/view/{item_id}")
def view_pdf(item_id: int):
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("SELECT title, file_data FROM du_resources WHERE id = ?", (item_id,))
    row = cursor.fetchone()
    conn.close()
    if row and row[1]:
        return StreamingResponse(
            io.BytesIO(row[1]),
            media_type="application/pdf",
            headers={
                "Content-Disposition": f'inline; filename="{row[0]}.pdf"',
                "X-Content-Type-Options": "nosniff",
                "Cache-Control": "public, max-age=604800, immutable"
            }
        )
    return HTMLResponse("Not Found", status_code=404)

@app.post("/delete/{item_id}")
def delete_item(request: Request, item_id: int):
    if request.cookies.get("du_admin_session") != "authenticated":
        return HTMLResponse("Unauthorized.", status_code=403)

    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("DELETE FROM du_resources WHERE id = ?", (item_id,))
    conn.commit()
    conn.close()
    return RedirectResponse(url="/", status_code=303)

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
