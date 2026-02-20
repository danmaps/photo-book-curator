import os
import json
import sqlite3
import zipfile
from datetime import datetime
from pathlib import Path
from typing import List, Optional

from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse, FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from PIL import Image, ExifTags

try:
    import pillow_heif
    pillow_heif.register_heif_opener()
except Exception:
    pass

PHOTO_ROOT = Path(os.getenv("PHOTO_ROOT", ""))
DATA_DIR = Path(os.getenv("DATA_DIR", str(Path(__file__).resolve().parents[1] / "data")))
DB_PATH = DATA_DIR / "photo_book.db"
THUMB_DIR = DATA_DIR / "thumbs"
BOOKS_PATH = Path(os.getenv("BOOKS_PATH", str(Path(__file__).resolve().parents[1] / "config" / "books.json")))

SUPPORTED_EXT = {".jpg", ".jpeg", ".png", ".heic"}

app = FastAPI(title="Photo Book Curator")

DATA_DIR.mkdir(parents=True, exist_ok=True)
THUMB_DIR.mkdir(parents=True, exist_ok=True)

app.mount("/thumbs", StaticFiles(directory=str(THUMB_DIR)), name="thumbs")


def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    conn = get_db()
    cur = conn.cursor()
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS photos (
            id INTEGER PRIMARY KEY,
            file_path TEXT UNIQUE,
            date_taken TEXT,
            thumbnail_path TEXT
        )
        """
    )
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS books (
            id TEXT PRIMARY KEY,
            child TEXT,
            month INTEGER,
            start_date TEXT,
            end_date TEXT,
            completed INTEGER DEFAULT 0
        )
        """
    )
    conn.commit()
    conn.close()


def load_books():
    if not BOOKS_PATH.exists():
        return []
    data = json.loads(BOOKS_PATH.read_text(encoding="utf-8"))
    conn = get_db()
    cur = conn.cursor()
    for b in data:
        cur.execute(
            """
            INSERT OR REPLACE INTO books (id, child, month, start_date, end_date, completed)
            VALUES (?, ?, ?, ?, ?, COALESCE((SELECT completed FROM books WHERE id = ?), 0))
            """,
            (b["id"], b["child"], b["month"], b["start_date"], b["end_date"], b["id"]),
        )
    conn.commit()
    conn.close()


def get_date_taken(path: Path) -> datetime:
    try:
        img = Image.open(path)
        exif = img.getexif()
        if exif:
            for k, v in ExifTags.TAGS.items():
                if v == "DateTimeOriginal":
                    date_tag = k
                    break
            else:
                date_tag = None
            if date_tag and date_tag in exif:
                return datetime.strptime(exif[date_tag], "%Y:%m:%d %H:%M:%S")
    except Exception:
        pass
    return datetime.fromtimestamp(path.stat().st_mtime)


def make_thumb(path: Path, photo_id: int) -> Path:
    thumb_path = THUMB_DIR / f"{photo_id}.jpg"
    if thumb_path.exists():
        return thumb_path
    img = Image.open(path)
    img.thumbnail((360, 360))
    img.convert("RGB").save(thumb_path, "JPEG", quality=85)
    return thumb_path


def index_photos():
    if not PHOTO_ROOT.exists():
        return
    conn = get_db()
    cur = conn.cursor()
    for root, _dirs, files in os.walk(PHOTO_ROOT):
        for f in files:
            ext = Path(f).suffix.lower()
            if ext not in SUPPORTED_EXT:
                continue
            full = Path(root) / f
            date_taken = get_date_taken(full)
            cur.execute(
                "INSERT OR IGNORE INTO photos (file_path, date_taken, thumbnail_path) VALUES (?, ?, '')",
                (str(full), date_taken.isoformat()),
            )
    conn.commit()

    cur.execute("SELECT id, file_path, thumbnail_path FROM photos WHERE thumbnail_path = ''")
    rows = cur.fetchall()
    for r in rows:
        thumb = make_thumb(Path(r["file_path"]), r["id"])
        cur.execute("UPDATE photos SET thumbnail_path = ? WHERE id = ?", (str(thumb), r["id"]))
    conn.commit()
    conn.close()


@app.on_event("startup")
def startup():
    init_db()
    load_books()
    if PHOTO_ROOT.exists():
        index_photos()


@app.get("/", response_class=HTMLResponse)
def home():
    return HTMLResponse(
        """
<!doctype html>
<html>
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>Photo Book Curator</title>
  <style>
    body { font-family: system-ui, -apple-system, Segoe UI, Roboto, sans-serif; margin: 0; padding: 16px; }
    h1 { margin: 0 0 10px 0; }
    .grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(140px, 1fr)); gap: 8px; }
    .card { border: 1px solid #ddd; border-radius: 10px; padding: 10px; }
    .book { display: flex; justify-content: space-between; }
    .done { color: #0a7; font-weight: 600; }
    button { padding: 10px 14px; border-radius: 8px; border: 1px solid #ccc; background: #fff; }
  </style>
</head>
<body>
  <h1>Photo Book Curator</h1>
  <div id="books" class="grid"></div>
  <script>
    async function load() {
      const res = await fetch('/api/books');
      const data = await res.json();
      const el = document.getElementById('books');
      el.innerHTML = data.map(b => `
        <div class="card">
          <div class="book">
            <div>${b.child} – Month ${String(b.month).padStart(2,'0')}</div>
            <div class="${b.completed ? 'done' : ''}">${b.completed ? 'Done' : ''}</div>
          </div>
          <div>${b.start_date} → ${b.end_date}</div>
          <div style="margin-top:8px;"><a href="/book/${b.id}">Open</a></div>
        </div>
      `).join('');
    }
    load();
  </script>
</body>
</html>
        """
    )


@app.get("/book/{book_id}", response_class=HTMLResponse)
def book_view(book_id: str):
    return HTMLResponse(
        f"""
<!doctype html>
<html>
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>Book {book_id}</title>
  <style>
    body {{ font-family: system-ui, -apple-system, Segoe UI, Roboto, sans-serif; margin: 0; padding: 16px; }}
    .grid {{ display: grid; grid-template-columns: repeat(auto-fill, minmax(140px, 1fr)); gap: 8px; }}
    .thumb {{ border: 2px solid transparent; border-radius: 8px; overflow: hidden; }}
    .thumb.selected {{ border-color: #4f81bd; }}
    img {{ width: 100%; height: auto; display: block; }}
    .bar {{ display: flex; gap: 10px; align-items: center; margin-bottom: 12px; }}
    button {{ padding: 10px 14px; border-radius: 8px; border: 1px solid #ccc; background: #fff; }}
  </style>
</head>
<body>
  <div class="bar">
    <a href="/">← Back</a>
    <span id="title"></span>
    <span id="count"></span>
    <button onclick="selectAll()">Select all visible</button>
    <button onclick="exportZip()">Export ZIP</button>
    <button onclick="markDone()">Mark done</button>
  </div>
  <div id="grid" class="grid"></div>

  <script>
    const bookId = "{book_id}";
    let selected = new Set();

    async function load() {{
      const res = await fetch(`/api/book/${bookId}`);
      const data = await res.json();
      document.getElementById('title').textContent = `${{data.child}} – Month ${{String(data.month).padStart(2,'0')}} (${data.start_date} → ${data.end_date})`;
      const grid = document.getElementById('grid');
      grid.innerHTML = data.photos.map(p => `
        <div class="thumb" data-id="${{p.id}}" onclick="toggle(${{p.id}})">
          <img src="/thumbs/${{p.thumbnail}}" />
        </div>
      `).join('');
      updateCount();
    }}

    function toggle(id) {{
      if (selected.has(id)) selected.delete(id); else selected.add(id);
      document.querySelector(`[data-id='${{id}}']`).classList.toggle('selected');
      updateCount();
    }}

    function selectAll() {{
      document.querySelectorAll('.thumb').forEach(el => {{
        const id = Number(el.getAttribute('data-id'));
        selected.add(id);
        el.classList.add('selected');
      }});
      updateCount();
    }}

    function updateCount() {{
      document.getElementById('count').textContent = `Selected: ${{selected.size}}`;
    }}

    async function exportZip() {{
      if (selected.size === 0) return;
      const res = await fetch(`/api/export/${{bookId}}`, {{
        method: 'POST',
        headers: {{ 'Content-Type': 'application/json' }},
        body: JSON.stringify({{ ids: Array.from(selected) }})
      }});
      const blob = await res.blob();
      const url = window.URL.createObjectURL(blob);
      const a = document.createElement('a');
      a.href = url;
      a.download = `book_${{bookId}}.zip`;
      a.click();
      window.URL.revokeObjectURL(url);
    }}

    async function markDone() {{
      await fetch(`/api/book/${{bookId}}/done`, {{ method: 'POST' }});
      alert('Marked done');
    }}

    load();
  </script>
</body>
</html>
        """
    )


@app.get("/api/books")
def api_books():
    conn = get_db()
    cur = conn.cursor()
    cur.execute("SELECT * FROM books ORDER BY child, month")
    rows = [dict(r) for r in cur.fetchall()]
    conn.close()
    return rows


@app.get("/api/book/{book_id}")
def api_book(book_id: str):
    conn = get_db()
    cur = conn.cursor()
    cur.execute("SELECT * FROM books WHERE id = ?", (book_id,))
    book = cur.fetchone()
    if not book:
        raise HTTPException(status_code=404, detail="Book not found")

    start = book["start_date"]
    end = book["end_date"]
    cur.execute(
        """
        SELECT id, thumbnail_path FROM photos
        WHERE date(date_taken) >= date(?) AND date(date_taken) <= date(?)
        ORDER BY date_taken ASC
        """,
        (start, end),
    )
    photos = [
        {"id": r["id"], "thumbnail": Path(r["thumbnail_path"]).name}
        for r in cur.fetchall()
    ]
    conn.close()
    return {
        **dict(book),
        "photos": photos,
    }


@app.post("/api/book/{book_id}/done")
def mark_done(book_id: str):
    conn = get_db()
    cur = conn.cursor()
    cur.execute("UPDATE books SET completed = 1 WHERE id = ?", (book_id,))
    conn.commit()
    conn.close()
    return {"ok": True}


@app.post("/api/export/{book_id}")
def export_zip(book_id: str, payload: dict):
    ids = payload.get("ids") or []
    if not ids:
        raise HTTPException(status_code=400, detail="No photo ids")

    conn = get_db()
    cur = conn.cursor()
    cur.execute("SELECT * FROM books WHERE id = ?", (book_id,))
    book = cur.fetchone()
    if not book:
        raise HTTPException(status_code=404, detail="Book not found")

    q = f"SELECT file_path FROM photos WHERE id IN ({','.join(['?']*len(ids))})"
    cur.execute(q, ids)
    files = [r[0] for r in cur.fetchall()]
    conn.close()

    name = f"{book['child']}_Month_{int(book['month']):02d}_{book['start_date']}_to_{book['end_date']}.zip"
    out_path = DATA_DIR / name
    with zipfile.ZipFile(out_path, "w", zipfile.ZIP_DEFLATED) as z:
        for f in files:
            z.write(f, arcname=Path(f).name)

    return FileResponse(out_path, filename=name)
