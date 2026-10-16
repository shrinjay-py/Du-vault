import os
import io
import re
import hmac
import time
import zlib
import html
import hashlib
import urllib.parse
import libsql_experimental as sqlite3
from fastapi import FastAPI, UploadFile, File, Form, Response, Request
from fastapi.responses import HTMLResponse, StreamingResponse, RedirectResponse
from fastapi.middleware.gzip import GZipMiddleware

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


def get_db():
    if TURSO_DB_URL and TURSO_AUTH_TOKEN:
        return sqlite3.connect(TURSO_DB_URL, auth_token=TURSO_AUTH_TOKEN)
    else:
        return sqlite3.connect("du_pyq_vault.db")


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

# ───────────────────────── SUBJECT CATALOGUE ─────────────────────────
# Course -> Semester -> subject names. EDIT THIS to match your exact DU syllabus.
# Subjects you type while uploading/editing that are not listed here are added automatically.
CURRICULUM = {
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
    ("non-chord", "🪼"), ("chordate", "🐟"), ("cell", "🔬"), ("algae", "🌊"), ("phyco", "🌊"),
    ("myco", "🍄"), ("pathology", "🍄"), ("archegoniate", "🌱"), ("angiosperm", "🌸"),
    ("anatomy", "🔍"), ("economic botany", "🌾"), ("econom", "📈"), ("ecolog", "🌍"),
    ("physiolog", "💧"), ("metabol", "⚗️"), ("biotech", "🧫"), ("systematic", "🗂️"),
    ("taxonom", "🗂️"), ("evolution", "🦴"), ("development", "🐣"), ("micro", "🦠"),
    ("molecul", "🧬"), ("genetic", "🧬"), ("bio", "🧬"), ("inorganic", "🧪"), ("organic", "⚗️"),
    ("chem", "🧪"), ("spectro", "🌈"), ("thermo", "🔥"), ("thermal", "🔥"), ("calculus", "∫"),
    ("algebra", "➕"), ("analysis", "📐"), ("group", "🔣"), ("ring", "🔣"), ("mathematical", "📐"),
    ("mechanic", "⚙️"), ("optic", "🔭"), ("wave", "🌊"), ("electr", "⚡"), ("magnet", "🧲"),
    ("digital", "🔌"), ("analog", "🔌"), ("quantum", "⚛️"), ("solid state", "💎"),
    ("program", "💻"), ("python", "🐍"), ("data", "🗃️"), ("discrete", "🔣"), ("operating", "🖥️"),
    ("network", "🌐"), ("algorithm", "🧠"), ("artificial", "🤖"), ("account", "🧾"),
    ("tax", "🧾"), ("law", "⚖️"), ("audit", "🧮"), ("market", "📣"), ("statist", "📊"),
    ("probab", "🎲"), ("econometric", "📉"), ("english", "📖"), ("politic", "🏛️"),
]


def subject_icon(name: str) -> str:
    low = name.lower()
    for kw, emoji in SUBJECT_ICONS:
        if kw in low:
            return emoji
    return "📘"


def subject_gradient(name: str):
    return GRADIENTS[zlib.crc32(name.encode()) % len(GRADIENTS)]


# ───────────────────────── DETECTION LOGIC ─────────────────────────
STOP_WORDS = {"and", "of", "the", "in", "to", "for", "with"}


def norm(text: str) -> str:
    text = text.lower().replace("&", " and ")
    return " " + re.sub(r"[^a-z0-9+]+", " ", text).strip() + " "


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
    t = norm(text)
    order = list(cur.get(sem, [])) + [s for sm, names in cur.items() if sm != sem for s in names]
    for name in order:
        if any(norm(k) in t for k in [name] + SUBJECT_ALIASES.get(name, [])):
            return name
    words = set(t.split())
    for name in order:
        toks = [w for w in norm(name).split() if w not in STOP_WORDS and len(w) > 3]
        if toks and all(w in words for w in toks):
            return name
    return UNSORTED


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


def parse_filename(filename: str, fallback_course: str, fallback_sem: str, fallback_year: str):
    lower = filename.lower()
    detected_year = fallback_year.strip() if fallback_year.strip() else None
    if not detected_year:
        year_match = re.search(r"\b(20[1-2][0-9])\b", lower)
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
        detected_course = detect_course(lower)
    base = os.path.splitext(filename)[0]
    clean_title = re.sub(r"[_]+", " ", base).strip().title()
    return clean_title, detected_course, detected_sem, detected_year


# ───────────────────────── DATABASE ─────────────────────────
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
    # One-time sort of old papers into subjects using their titles
    cursor.execute("SELECT id, title, course, semester FROM du_resources WHERE subject IS NULL OR subject = ''")
    rows = cursor.fetchall()
    for rid, title, course, sem in rows:
        cursor.execute("UPDATE du_resources SET subject = ? WHERE id = ?", (detect_subject(course, sem, title), rid))
    if rows:
        conn.commit()
    conn.close()


init_db()

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

SERVICE_WORKER_JS = """const CACHE_NAME = 'du-vault-cache-v18';
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
  // Pages: network first (so login/logout and new uploads show instantly), cache as offline fallback
  if (e.request.mode === 'navigate') {
    e.respondWith(
      fetch(e.request).then((res) => {
        if (res && res.ok) {
          const copy = res.clone();
          caches.open(CACHE_NAME).then((c) => c.put(e.request, copy));
        }
        return res;
      }).catch(() => caches.match(e.request).then((r) => r || caches.match('/')))
    );
    return;
  }
  // Everything else: serve cache instantly, refresh in background
  e.respondWith(
    caches.match(e.request).then((cachedResponse) => {
      if (cachedResponse) {
        fetch(e.request).then((networkResponse) => {
          if (networkResponse && networkResponse.status === 200) {
            caches.open(CACHE_NAME).then((cache) => cache.put(e.request, networkResponse));
          }
        }).catch(() => {});
        return cachedResponse;
      }
      return fetch(e.request).then((networkResponse) => {
        return caches.open(CACHE_NAME).then((cache) => {
          cache.put(e.request, networkResponse.clone());
          return networkResponse;
        });
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
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&display=swap" rel="stylesheet">
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
.pdf-canvas-wrap { margin: 0 auto 16px auto; box-shadow: 0 4px 20px rgba(0,0,0,0.6); border-radius: 4px; background: #FFFFFF; line-height: 0; }
.pdf-page-canvas { display: block; border-radius: 4px; pointer-events: none; }
.pdf-spinner { width: 44px; height: 44px; border: 3px solid rgba(167, 122, 83, 0.25); border-top: 3px solid var(--caramel); border-radius: 50%; animation: spinRing 0.9s linear infinite; margin-bottom: 14px; }
@media (max-width: 480px) {
    .subject-grid { grid-template-columns: repeat(2, 1fr); }
    .cards-layout-grid { grid-template-columns: 1fr; }
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
            await page.render({ canvasContext: context, viewport: highResViewport }).promise;
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
def subject_card_html(course: str, name: str, count: int) -> str:
    g1, g2 = subject_gradient(name)
    href = f"/?course={urlq(course)}&subject={urlq(name)}"
    label = "paper" if count == 1 else "papers"
    return f"""
    <a href="{esc(href)}" class="subject-card" style="--g1:{g1};--g2:{g2}">
        <div class="subject-icon"><span>{subject_icon(name)}</span></div>
        <div class="subject-title">{esc(name)}</div>
        <div class="subject-count">{count} {label}</div>
    </a>
    """


def crumbs_html(course: str, subject: str, q: str) -> str:
    parts = ['<a href="/">Home</a>']
    if course:
        tile = next((t for t in COURSE_TILES if t["name"] == course), None)
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
def index(request: Request, q: str = "", course: str = "", sem: str = "All Semesters", subject: str = ""):
    admin = is_admin(request)
    q = q.strip()
    conn = get_db()
    cursor = conn.cursor()
    main_view_content = ""

    # ── LEVEL 1: Home → course folders
    if not course and not q and not subject:
        cursor.execute("SELECT course, COUNT(*) FROM du_resources GROUP BY course")
        counts = dict(cursor.fetchall())
        folders_grid = '<div style="font-size: 0.85rem; font-weight: 700; text-transform: uppercase; color: var(--text-muted); margin-bottom: 8px;">Select Course Folder</div>'
        folders_grid += '<div class="folder-grid">'
        for tile in COURSE_TILES:
            num_papers = counts.get(tile["name"], 0)
            folders_grid += f"""
            <a href="/?course={esc(urlq(tile["name"]))}" class="folder-card">
                <div class="folder-card-icon">{tile["icon"]}</div>
                <div class="folder-card-title">{esc(tile["label"])}</div>
                <div class="folder-card-count">{num_papers} papers</div>
            </a>
            """
        folders_grid += "</div>"
        search_bar = """
        <form method="GET" action="/">
            <div class="search-box" style="margin-top: 10px;">
                <input type="text" name="q" placeholder="🔍 Search any paper across all courses...">
            </div>
        </form>
        """
        main_view_content = search_bar + folders_grid

    # ── LEVEL 2: Course → subject folders (grouped by semester)
    elif course and not subject and not q:
        cursor.execute(
            "SELECT subject, semester, COUNT(*) FROM du_resources WHERE course = ? GROUP BY subject, semester",
            (course,),
        )
        rows = cursor.fetchall()
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

        tile = next((t for t in COURSE_TILES if t["name"] == course), None)
        icon = tile["icon"] if tile else "📁"
        label = tile["label"] if tile else course
        header_bar = f"""
        <div class="folder-header-bar">
            <div class="folder-header-title"><span>{icon}</span><span>{esc(label)}</span></div>
            <a href="/" class="back-folder-btn">← All Courses</a>
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
            cards = "".join(subject_card_html(course, n, sub_counts.get(n, 0)) for n in names)
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
        query = "SELECT id, title, course, semester, year, type, url_or_name, file_size, subject FROM du_resources WHERE 1=1"
        params = []
        if course:
            query += " AND course = ?"
            params.append(course)
        if subject:
            query += " AND subject = ?"
            params.append(subject)
        if sem != "All Semesters":
            query += " AND semester = ?"
            params.append(sem)
        if q:
            query += " AND (LOWER(title) LIKE ? OR LOWER(course) LIKE ? OR LOWER(year) LIKE ? OR LOWER(subject) LIKE ?)"
            wc = f"%{q.lower()}%"
            params.extend([wc, wc, wc, wc])
        query += " ORDER BY year DESC, id DESC"
        cursor.execute(query, tuple(params))
        records = cursor.fetchall()

        tile = next((t for t in COURSE_TILES if t["name"] == course), None)
        if subject:
            folder_icon = subject_icon(subject)
            folder_display_name = subject
            back_href = f"/?course={urlq(course)}" if course else "/"
            back_label = "← Subjects" if course else "← All Folders"
        else:
            folder_icon = tile["icon"] if tile else "🔍"
            folder_display_name = (tile["label"] if tile else course) if course else f"Search: {q}"
            back_href = f"/?course={urlq(course)}" if (course and q) else "/"
            back_label = "← Subjects" if (course and q) else "← All Folders"
        header_bar = f"""
        <div class="folder-header-bar">
            <div class="folder-header-title"><span>{folder_icon}</span><span>{esc(folder_display_name)}</span></div>
            <a href="{esc(back_href)}" class="back-folder-btn">{back_label}</a>
        </div>
        """
        sem_options = "".join(
            f'<option value="{esc(s)}" {"selected" if s == sem else ""}>{esc(s)}</option>' for s in SEMESTERS
        )
        search_filter_form = f"""
        <form method="GET" action="/">
            <input type="hidden" name="course" value="{esc(course)}">
            <input type="hidden" name="subject" value="{esc(subject)}">
            <div class="search-box">
                <input type="text" name="q" value="{esc(q)}" placeholder="🔍 Live search in this list..." onkeyup="filterCardsLive(this.value)">
            </div>
            <div class="filters"><select name="sem" onchange="this.form.submit()">{sem_options}</select></div>
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

    # Subject suggestions for the upload / edit forms
    cursor.execute("SELECT DISTINCT subject FROM du_resources WHERE subject IS NOT NULL AND subject != ''")
    known = {r[0] for r in cursor.fetchall()}
    conn.close()
    for sems in CURRICULUM.values():
        for names in sems.values():
            known.update(names)
    known.discard(UNSORTED)
    subject_datalist = "".join(f'<option value="{esc(n)}"></option>' for n in sorted(known))

    up_course_opts = "".join(f'<option value="{esc(c)}">{esc(c)}</option>' for c in COURSES[1:])
    up_sem_opts = "".join(f'<option value="{esc(s)}">{esc(s)}</option>' for s in SEMESTERS[1:])
    if admin:
        admin_header_btn = '<a href="/logout" class="admin-lock-btn">Logout</a>'
        admin_banner_html = '<div class="admin-banner"><span>⚡ Host Controls Unlocked</span><a href="/logout">Lock</a></div>'
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
    content = content.replace("{admin_header_btn}", admin_header_btn)
    content = content.replace("{admin_banner_html}", admin_banner_html)
    content = content.replace("{fab_controls}", fab_controls)
    content = content.replace("{main_view_content}", main_view_content)
    return HTMLResponse(content=content)


# ───────────────────────── AUTH ROUTES ─────────────────────────
@app.post("/login")
def login(password: str = Form(...)):
    if hmac.compare_digest(password.encode(), ADMIN_PASSWORD.encode()):
        response = RedirectResponse(url="/", status_code=303)
        response.set_cookie(
            key="du_admin_session",
            value=make_session_token(),
            httponly=True,
            secure=COOKIE_SECURE,
            samesite="lax",
            max_age=SESSION_TTL,
        )
        return response
    return HTMLResponse("Invalid Password", status_code=401)


@app.get("/logout")
def logout():
    response = RedirectResponse(url="/", status_code=303)
    response.delete_cookie(key="du_admin_session")
    return response


# ───────────────────────── ADMIN ACTIONS ─────────────────────────
def folder_redirect(course: str, subject: str = "") -> RedirectResponse:
    url = f"/?course={urlq(course)}"
    if subject:
        url += f"&subject={urlq(subject)}"
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
    if course != "auto" and course not in COURSES[1:]:
        course = "auto"
    if sem != "auto" and sem not in SEMESTERS[1:]:
        sem = "auto"
    conn = get_db()
    cursor = conn.cursor()
    last_course, last_subject = None, ""
    for file in files:
        if file.filename and file.filename.lower().endswith(".pdf"):
            data = await file.read()
            size_mb = f"{len(data) / (1024 * 1024):.2f} MB"
            clean_title, detected_course, detected_sem, detected_year = parse_filename(file.filename, course, sem, year)
            detected_subject = resolve_subject(detected_course, detected_sem, subject, file.filename)
            last_course, last_subject = detected_course, detected_subject
            cursor.execute(
                """
                INSERT INTO du_resources (title, course, semester, year, type, url_or_name, file_data, file_size, subject)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (clean_title, detected_course, detected_sem, detected_year, "pdf", file.filename, data, size_mb, detected_subject),
            )
    conn.commit()
    conn.close()
    if not last_course:
        return RedirectResponse(url="/", status_code=303)
    # Single subject in the batch → open that subject; otherwise open the course
    return folder_redirect(last_course, last_subject if subject.strip() else "")


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
    if course not in COURSES[1:] or sem not in SEMESTERS[1:]:
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
    if course not in COURSES[1:] or sem not in SEMESTERS[1:]:
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
    conn.commit()
    conn.close()
    return folder_redirect(course, final_subject)


@app.get("/view/{item_id}")
def view_pdf(item_id: int):
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("SELECT title, file_data FROM du_resources WHERE id = ?", (item_id,))
    row = cursor.fetchone()
    conn.close()
    if row and row[1]:
        safe_name = urllib.parse.quote(f"{row[0]}.pdf")
        return StreamingResponse(
            io.BytesIO(row[1]),
            media_type="application/pdf",
            headers={
                "Content-Disposition": f"inline; filename*=UTF-8''{safe_name}",
                "X-Content-Type-Options": "nosniff",
                "Cache-Control": "public, max-age=604800, immutable",
            },
        )
    return HTMLResponse("Not Found", status_code=404)


@app.post("/delete/{item_id}")
def delete_item(request: Request, item_id: int):
    if not is_admin(request):
        return HTMLResponse("Unauthorized.", status_code=403)
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("DELETE FROM du_resources WHERE id = ?", (item_id,))
    conn.commit()
    conn.close()
    return RedirectResponse(url=back_url(request), status_code=303)


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
