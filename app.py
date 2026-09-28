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
    {"name": "B.Tech / CIC", "icon": "🧲", "label": "Physical science with chem"}
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
            url_or_name TEXT,
            file_data BLOB,
            file_size TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
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
  "orientation": "portrait-primary",
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

SERVICE_WORKER_JS = """const CACHE_NAME = 'du-vault-cache-v5';
const PRECACHE = ['/', '/manifest.json', '/icon.svg'];

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
  e.respondWith(
    fetch(e.request).catch(async () => {
      const cached = await caches.match(e.request);
      return cached || (e.request.mode === 'navigate' ? caches.match('/') : null);
    })
  );
});"""

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
    <meta name="viewport" content="width=device-width, initial-scale=1.0, maximum-scale=1.0, user-scalable=no">
    <title>DU PYQ Vault</title>
    
    <link rel="manifest" href="/manifest.json">
    <meta name="theme-color" content="#1E1A17">
    <meta name="apple-mobile-web-app-capable" content="yes">
    <meta name="apple-mobile-web-app-status-bar-style" content="black-translucent">
    <link rel="icon" type="image/svg+xml" href="/icon.svg">
    <link rel="apple-touch-icon" href="/icon.svg">

    <link href="https://fonts.googleapis.com/css2?family=Georgia&family=Inter:wght@400;500;600;700&display=swap" rel="stylesheet">
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
        header {
            background: var(--espresso);
            color: #FAF6F2;
            padding: 14px 20px;
            position: sticky; top: 0; z-index: 50;
            display: flex;
            align-items: center;
            justify-content: space-between;
        }
        .brand-title { 
            font-family: 'Georgia', serif; 
            font-size: 1.15rem; 
            font-weight: bold; 
            letter-spacing: 0.5px;
            text-decoration: none;
            color: #FAF6F2;
        }
        .brand-right {
            display: flex;
            align-items: center;
            gap: 14px;
        }
        .founder-tag {
            font-family: 'Georgia', serif;
            font-size: 0.9rem;
            color: #FAF6F2;
            opacity: 0.95;
            text-align: center;
            line-height: 1.15;
            display: flex;
            flex-direction: column;
            align-items: center;
        }
        .founder-tag span.founder-name {
            color: var(--caramel);
            font-weight: 700;
            font-size: 0.95rem;
            letter-spacing: 0.3px;
        }
        .college-subtag {
            font-size: 0.48rem;
            color: #BFA898;
            font-family: 'Inter', sans-serif;
            font-weight: 500;
            font-style: normal;
            letter-spacing: 0.5px;
            text-transform: uppercase;
            text-align: center;
            margin-top: 2px;
            opacity: 0.8;
            display: block;
        }
        .admin-lock-btn {
            background: transparent;
            border: 1px solid rgba(255,255,255,0.25);
            color: #FAF6F2;
            border-radius: 12px;
            padding: 4px 8px;
            font-size: 0.75rem;
            cursor: pointer;
            text-decoration: none;
        }
        .admin-banner {
            background: #D4A373;
            color: #1E1A17;
            padding: 6px 16px;
            font-size: 0.8rem;
            font-weight: 600;
            display: flex;
            justify-content: space-between;
            align-items: center;
        }
        .admin-banner a {
            color: #641E16;
            text-decoration: underline;
            cursor: pointer;
        }
        .container { max-width: 650px; margin: 0 auto; padding: 16px; }

        /* PWA Install Banner */
        .install-box {
            display: none;
            background: var(--espresso);
            color: #FAF6F2;
            padding: 12px 16px;
            border-radius: 16px;
            margin-bottom: 14px;
            align-items: center;
            justify-content: space-between;
            box-shadow: 0 4px 14px rgba(0,0,0,0.12);
        }
        .install-text {
            font-size: 0.84rem;
            font-weight: 600;
            display: flex;
            align-items: center;
            gap: 8px;
        }
        .install-btn {
            background: var(--caramel);
            color: #FAF6F2;
            border: none;
            padding: 7px 15px;
            border-radius: 12px;
            font-weight: 700;
            font-size: 0.78rem;
            cursor: pointer;
        }

        /* Course Folder Grid */
        .folder-grid {
            display: grid;
            grid-template-columns: repeat(auto-fill, minmax(130px, 1fr));
            gap: 12px;
            margin: 12px 0 20px 0;
        }
        .folder-card {
            text-decoration: none;
            color: var(--text-dark);
            background: var(--card-foam);
            border: 1px solid var(--border-latte);
            border-radius: 16px;
            padding: 18px 12px;
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
        .folder-card:active { transform: scale(0.97); }
        .folder-card-icon { font-size: 2.2rem; margin-bottom: 8px; }
        .folder-card-title { font-size: 0.82rem; font-weight: 700; text-align: center; }
        .folder-card-count { font-size: 0.72rem; color: var(--text-muted); margin-top: 4px; }

        /* Inside Folder Directory Bar */
        .folder-header-bar {
            display: flex;
            align-items: center;
            justify-content: space-between;
            background: var(--card-foam);
            border: 1px solid var(--border-latte);
            border-radius: 16px;
            padding: 14px 18px;
            margin-bottom: 14px;
        }
        .folder-header-title {
            display: flex;
            align-items: center;
            gap: 10px;
            font-family: 'Georgia', serif;
            font-size: 1.1rem;
            font-weight: bold;
        }
        .back-folder-btn {
            background: var(--espresso);
            color: #FAF6F2;
            text-decoration: none;
            padding: 8px 14px;
            border-radius: 12px;
            font-size: 0.78rem;
            font-weight: 600;
        }

        .search-box input {
            width: 100%; padding: 12px 18px; border-radius: 25px;
            border: 1px solid var(--border-latte); background: var(--card-foam);
            font-size: 0.95rem; outline: none; margin-bottom: 12px;
        }
        .filters { display: flex; gap: 8px; margin-bottom: 16px; }
        select {
            flex: 1; padding: 10px; border-radius: 18px;
            border: 1px solid var(--border-latte); background: var(--card-foam);
            font-size: 0.85rem; outline: none;
        }
        .card {
            background: var(--card-foam); border: 1px solid var(--border-latte);
            border-radius: 18px; padding: 16px; margin-bottom: 12px;
        }
        .card-top { display: flex; justify-content: space-between; align-items: center; margin-bottom: 6px; }
        .badge { font-size: 0.75rem; font-weight: 700; color: var(--caramel); }
        .del-btn { background: none; border: none; color: #BA1A1A; font-size: 1.1rem; cursor: pointer; padding: 0 4px; }
        .card-title { font-family: 'Georgia', serif; font-size: 1.05rem; font-weight: bold; margin-bottom: 6px; }
        .card-meta { font-size: 0.8rem; color: var(--text-muted); margin-bottom: 14px; }
        .btn-pill {
            display: inline-block; text-align: center; width: 100%; padding: 10px 0;
            background: var(--espresso); color: #FAF6F2; text-decoration: none;
            font-size: 0.85rem; font-weight: 600; border-radius: 20px; border: none; cursor: pointer;
        }
        .btn-caramel { background: var(--caramel); }
        .fab-bar {
            position: fixed; bottom: 20px; left: 50%; transform: translateX(-50%);
            display: flex; gap: 10px; z-index: 100;
        }
        .fab {
            padding: 12px 20px; border-radius: 30px; background: var(--espresso);
            color: #FAF6F2; border: none; font-size: 0.9rem; font-weight: 600;
            box-shadow: 0 4px 16px rgba(0,0,0,0.25); cursor: pointer;
        }
        .modal {
            display: none; position: fixed; top: 0; left: 0; right: 0; bottom: 0;
            background: rgba(30,26,23,0.65); z-index: 200; align-items: flex-end; justify-content: center;
        }
        .modal.active { display: flex; }
        .modal-content {
            background: var(--card-foam); width: 100%; max-width: 500px;
            border-radius: 24px 24px 0 0; padding: 24px; max-height: 85vh; overflow-y: auto;
        }
        .modal-title { font-family: 'Georgia', serif; font-size: 1.2rem; font-weight: bold; margin-bottom: 14px; }
        .form-group { margin-bottom: 12px; }
        .form-group label { display: block; font-size: 0.8rem; font-weight: 600; margin-bottom: 4px; }
        .form-group input, .form-group select {
            width: 100%; padding: 10px; border-radius: 12px;
            border: 1px solid var(--border-latte); background: var(--bg-latte); outline: none;
        }
        .helper-text { font-size: 0.72rem; color: var(--text-muted); margin-top: 3px; }
    </style>
</head>
<body>

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
    <div id="pwa-install-banner" class="install-box">
        <div class="install-text"><span>📲</span> Install DU Vault App</div>
        <button id="pwa-install-btn" class="install-btn">Install</button>
    </div>

    {main_view_content}
</div>

{fab_controls}

<!-- BATCH UPLOAD MODAL -->
<div class="modal" id="uploadModal" onclick="if(event.target === this) closeModal('uploadModal')">
    <div class="modal-content">
        <div class="modal-title">Batch Upload PDFs</div>
        <form action="/upload" method="POST" enctype="multipart/form-data">
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

<!-- LINK MODAL -->
<div class="modal" id="linkModal" onclick="if(event.target === this) closeModal('linkModal')">
    <div class="modal-content">
        <div class="modal-title">Add Reference Link</div>
        <form action="/add-link" method="POST">
            <div class="form-group"><label>Title</label><input type="text" name="title" placeholder="e.g. Official Syllabus" required></div>
            <div class="form-group"><label>URL</label><input type="url" name="url" placeholder="https://..." required></div>
            <div class="form-group"><label>Course</label><select name="course" required>{upload_course_options}</select></div>
            <div class="form-group"><label>Semester</label><select name="sem" required>{upload_sem_options}</select></div>
            <button type="submit" class="btn-pill btn-caramel" style="margin-top: 8px;">Save Link</button>
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
    function openModal(id) { document.getElementById(id).classList.add('active'); }
    function closeModal(id) { document.getElementById(id).classList.remove('active'); }

    if ('serviceWorker' in navigator) {
        window.addEventListener('load', () => {
            navigator.serviceWorker.register('/sw.js', { scope: '/' });
        });
    }

    let deferredPrompt;
    window.addEventListener('beforeinstallprompt', (e) => {
        e.preventDefault();
        deferredPrompt = e;
        const banner = document.getElementById('pwa-install-banner');
        if (banner) banner.style.display = 'flex';
    });

    const installBtn = document.getElementById('pwa-install-btn');
    if (installBtn) {
        installBtn.addEventListener('click', async () => {
            if (deferredPrompt) {
                deferredPrompt.prompt();
                const { outcome } = await deferredPrompt.userChoice;
                if (outcome === 'accepted') {
                    const banner = document.getElementById('pwa-install-banner');
                    if (banner) banner.style.display = 'none';
                }
                deferredPrompt = null;
            }
        });
    }

    window.addEventListener('appinstalled', () => {
        const banner = document.getElementById('pwa-install-banner');
        if (banner) banner.style.display = 'none';
    });
</script>
</body>
</html>
"""

def parse_filename(filename: str, fallback_course: str, fallback_sem: str, fallback_year: str):
    """Auto-detects course, semester, and year from the PDF filename."""
    lower = filename.lower()
    
    # 1. Detect Year
    detected_year = fallback_year.strip() if fallback_year.strip() else None
    if not detected_year:
        year_match = re.search(r'\b(20[1-2][0-9])\b', lower)
        detected_year = year_match.group(1) if year_match else "2024"

    # 2. Detect Semester
    detected_sem = fallback_sem
    if fallback_sem == "auto":
        sem_match = re.search(r'(?:sem(?:ester)?[\s_-]*([1-6])|\bs([1-6])\b)', lower)
        if sem_match:
            digit = sem_match.group(1) or sem_match.group(2)
            detected_sem = f"Sem {digit}"
        else:
            detected_sem = "Sem 1"

    # 3. Detect Course
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
def index(request: Request, q: str = "", course: str = "", sem: str = "All Semesters"):
    is_admin = request.cookies.get("du_admin_session") == "authenticated"
    conn = get_db()
    cursor = conn.cursor()

    # Get resource counts per course for folders
    cursor.execute("SELECT course, COUNT(*) FROM du_resources GROUP BY course")
    counts = dict(cursor.fetchall())

    main_view_content = ""

    # SCENARIO 1: HOME PAGE (Clean Folder Drive Interface)
    if not course and not q.strip():
        folders_grid = '<div style="font-size: 0.82rem; font-weight: 700; text-transform: uppercase; color: var(--text-muted); margin-bottom: 6px;">Select Course Folder</div>'
        folders_grid += '<div class="folder-grid">'
        for tile in COURSE_TILES:
            encoded_c = urllib.parse.quote_plus(tile["name"])
            num_papers = counts.get(tile["name"], 0)
            folders_grid += f"""
            <a href="/?course={encoded_c}" class="folder-card">
                <div class="folder-card-icon">{tile["icon"]}</div>
                <div class="folder-card-title">{tile["label"]}</div>
                <div class="folder-card-count">{num_papers} papers</div>
            </a>
            """
        folders_grid += '</div>'

        search_bar = """
        <form method="GET" action="/">
            <div class="search-box" style="margin-top: 10px;">
                <input type="text" name="q" placeholder="🔍 Search any paper across all courses...">
            </div>
        </form>
        """
        main_view_content = search_bar + folders_grid

    # SCENARIO 2: INSIDE A SPECIFIC FOLDER OR PERFORMING A GLOBAL SEARCH
    else:
        query = "SELECT id, title, course, semester, year, type, url_or_name, file_size FROM du_resources WHERE 1=1"
        params = []

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

        # Find Icon for folder header
        matched_tile = next((t for t in COURSE_TILES if t["name"] == course), None)
        folder_icon = matched_tile["icon"] if matched_tile else "📁"
        folder_display_name = matched_tile["label"] if matched_tile else (course or f"Search: '{q}'")

        header_bar = f"""
        <div class="folder-header-bar">
            <div class="folder-header-title">
                <span>{folder_icon}</span>
                <span>{folder_display_name}</span>
            </div>
            <a href="/" class="back-folder-btn">← All Folders</a>
        </div>
        """

        search_filter_form = f"""
        <form method="GET" action="/">
            <input type="hidden" name="course" value="{course}">
            <div class="search-box">
                <input type="text" name="q" value="{q}" placeholder="🔍 Search inside this folder..." onchange="this.form.submit()">
            </div>
            <div class="filters">
                <select name="sem" onchange="this.form.submit()">{ "".join(f'<option value="{s}" {"selected" if s == sem else ""}>{s}</option>' for s in SEMESTERS) }</select>
            </div>
        </form>
        """

        cards_html = ""
        if not records:
            cards_html = f'<div class="card" style="text-align:center; padding:36px;"><p style="color:var(--text-muted);">No papers found inside this folder.</p></div>'
        else:
            for item_id, title, c, s, y, r_type, url_or_name, size in records:
                if r_type == "pdf":
                    action_btn = f"""
                    <div style="display: flex; gap: 8px; margin-top: 8px;">
                        <a class="btn-pill" href="/view/{item_id}" target="_blank" style="flex: 1; text-align: center;">👁️ View</a>
                        <a class="btn-pill btn-caramel" href="/download/{item_id}" style="flex: 1; text-align: center;">📥 Save</a>
                    </div>
                    """
                else:
                    action_btn = f'<a class="btn-pill btn-caramel" href="{url_or_name}" target="_blank">🔗 Open Link</a>'

                del_form = ""
                if is_admin:
                    del_form = f"""
                    <form action="/delete/{item_id}" method="POST" onsubmit="return confirm('Delete paper?');">
                        <button type="submit" class="del-btn">✕</button>
                    </form>
                    """

                cards_html += f"""
                <div class="card">
                    <div class="card-top">
                        <span class="badge">{c}</span>
                        {del_form}
                    </div>
                    <div class="card-title">{title}</div>
                    <div class="card-meta">🎓 {s} &nbsp;•&nbsp; 📅 {y} &nbsp;•&nbsp; 💾 {size}</div>
                    {action_btn}
                </div>
                """

        main_view_content = header_bar + search_filter_form + cards_html

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
                INSERT INTO du_resources (title, course, semester, year, type, url_or_name, file_data, file_size)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """, (clean_title, detected_course, detected_sem, detected_year, "pdf", file.filename, data, size_mb))
    conn.commit()
    conn.close()

    redirect_url = f"/?course={urllib.parse.quote_plus(last_detected_course)}" if last_detected_course else "/"
    return RedirectResponse(url=redirect_url, status_code=303)

@app.post("/add-link")
def add_link(
    request: Request,
    title: str = Form(...),
    url: str = Form(...),
    course: str = Form(...),
    sem: str = Form(...)
):
    if request.cookies.get("du_admin_session") != "authenticated":
        return HTMLResponse("Unauthorized.", status_code=403)

    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("""
        INSERT INTO du_resources (title, course, semester, year, type, url_or_name, file_data, file_size)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
    """, (title, course, sem, "Web", "link", url, None, "Link"))
    conn.commit()
    conn.close()
    return RedirectResponse(url=f"/?course={urllib.parse.quote_plus(course)}", status_code=303)

@app.get("/download/{item_id}")
def download_pdf(item_id: int):
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
                "Content-Disposition": f'attachment; filename="{row[0]}.pdf"',
                "X-Content-Type-Options": "nosniff",
                "Cache-Control": "public, max-age=604800, immutable"
            }
        )
    return HTMLResponse("Not Found", status_code=404)

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
