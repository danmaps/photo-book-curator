import json
import os
import sqlite3
import tempfile
import threading
import zipfile
from datetime import datetime
from pathlib import Path
from typing import Any

from fastapi import BackgroundTasks, FastAPI, HTTPException, Query
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles
from PIL import ExifTags, Image

try:
    import pillow_heif

    pillow_heif.register_heif_opener()
except Exception:
    pass


PHOTO_ROOT = Path(os.getenv("PHOTO_ROOT", ""))
DATA_DIR = Path(os.getenv("DATA_DIR", str(Path(__file__).resolve().parents[1] / "data")))
DB_PATH = DATA_DIR / "photo_book.db"
THUMB_DIR = DATA_DIR / "thumbs"
BOOKS_PATH = Path(
    os.getenv(
        "BOOKS_PATH",
        str(Path(__file__).resolve().parents[1] / "config" / "books.json"),
    )
)

SUPPORTED_EXT = {".jpg", ".jpeg", ".png", ".heic"}
INDEX_BATCH_SIZE = 50

app = FastAPI(title="Photo Book Curator")

DATA_DIR.mkdir(parents=True, exist_ok=True)
THUMB_DIR.mkdir(parents=True, exist_ok=True)

app.mount("/thumbs", StaticFiles(directory=str(THUMB_DIR)), name="thumbs")


_index_lock = threading.Lock()
_index_state: dict[str, Any] = {
    "state": "idle",
    "started_at": None,
    "finished_at": None,
    "indexed": 0,
    "new": 0,
    "updated": 0,
    "removed": 0,
    "errors": 0,
    "message": "",
}
_book_load_warnings: list[str] = []


def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def _set_index_state(**updates: Any):
    with _index_lock:
        _index_state.update(updates)


def _get_index_state() -> dict[str, Any]:
    with _index_lock:
        return dict(_index_state)


def _ensure_column(cur: sqlite3.Cursor, table: str, column: str, definition: str):
    cur.execute(f"PRAGMA table_info({table})")
    cols = {r[1] for r in cur.fetchall()}
    if column not in cols:
        cur.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")


def init_db():
    conn = get_db()
    cur = conn.cursor()

    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS photos (
            id INTEGER PRIMARY KEY,
            file_path TEXT UNIQUE,
            date_taken TEXT,
            thumbnail_path TEXT,
            file_mtime REAL,
            file_size INTEGER
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
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS selections (
            book_id TEXT,
            photo_id INTEGER,
            selected INTEGER NOT NULL DEFAULT 1,
            updated_at TEXT NOT NULL,
            PRIMARY KEY (book_id, photo_id)
        )
        """
    )

    _ensure_column(cur, "photos", "file_mtime", "REAL")
    _ensure_column(cur, "photos", "file_size", "INTEGER")

    cur.execute("CREATE INDEX IF NOT EXISTS idx_photos_date ON photos(date_taken)")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_selections_book ON selections(book_id)")

    conn.commit()
    conn.close()


def load_books():
    global _book_load_warnings
    _book_load_warnings = []

    if not BOOKS_PATH.exists():
        _book_load_warnings = [f"Books config not found at {BOOKS_PATH}"]
        return []

    try:
        data = json.loads(BOOKS_PATH.read_text(encoding="utf-8"))
    except Exception as exc:
        _book_load_warnings = [f"Failed to parse books config: {exc}"]
        return []

    if not isinstance(data, list):
        _book_load_warnings = ["Books config must be a JSON array"]
        return []

    valid_books = []
    seen_ids: set[str] = set()

    for item in data:
        if not isinstance(item, dict):
            _book_load_warnings.append("Skipped invalid book entry: not an object")
            continue

        required = {"id", "child", "month", "start_date", "end_date"}
        if not required.issubset(item):
            _book_load_warnings.append(f"Skipped book missing fields: {item}")
            continue

        book_id = str(item["id"]).strip()
        if not book_id:
            _book_load_warnings.append("Skipped book with empty id")
            continue
        if book_id in seen_ids:
            _book_load_warnings.append(f"Skipped duplicate book id: {book_id}")
            continue

        try:
            month = int(item["month"])
            start_date = datetime.strptime(str(item["start_date"]), "%Y-%m-%d").date()
            end_date = datetime.strptime(str(item["end_date"]), "%Y-%m-%d").date()
        except Exception:
            _book_load_warnings.append(f"Skipped book with invalid date/month: {book_id}")
            continue

        if not (1 <= month <= 12):
            _book_load_warnings.append(f"Skipped book with month outside 1-12: {book_id}")
            continue
        if start_date > end_date:
            _book_load_warnings.append(f"Skipped book with start_date > end_date: {book_id}")
            continue

        seen_ids.add(book_id)
        valid_books.append(
            {
                "id": book_id,
                "child": str(item["child"]).strip(),
                "month": month,
                "start_date": str(start_date),
                "end_date": str(end_date),
            }
        )

    conn = get_db()
    cur = conn.cursor()
    for book in valid_books:
        cur.execute(
            """
            INSERT INTO books (id, child, month, start_date, end_date)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                child = excluded.child,
                month = excluded.month,
                start_date = excluded.start_date,
                end_date = excluded.end_date
            """,
            (
                book["id"],
                book["child"],
                book["month"],
                book["start_date"],
                book["end_date"],
            ),
        )
    conn.commit()
    conn.close()
    return valid_books


def get_date_taken(path: Path) -> datetime:
    try:
        with Image.open(path) as img:
            exif = img.getexif()
            if exif:
                date_tag = None
                for key, value in ExifTags.TAGS.items():
                    if value == "DateTimeOriginal":
                        date_tag = key
                        break
                if date_tag and date_tag in exif:
                    return datetime.strptime(exif[date_tag], "%Y:%m:%d %H:%M:%S")
    except Exception:
        pass
    return datetime.fromtimestamp(path.stat().st_mtime)


def make_thumb(path: Path, photo_id: int) -> Path:
    thumb_path = THUMB_DIR / f"{photo_id}.jpg"
    if thumb_path.exists():
        return thumb_path
    with Image.open(path) as img:
        img.thumbnail((360, 360))
        img.convert("RGB").save(thumb_path, "JPEG", quality=85)
    return thumb_path


def index_photos(force: bool = False):
    if not PHOTO_ROOT.exists():
        _set_index_state(
            state="error",
            finished_at=datetime.utcnow().isoformat(),
            message=f"PHOTO_ROOT does not exist: {PHOTO_ROOT}",
        )
        return

    conn = get_db()
    cur = conn.cursor()

    cur.execute("SELECT id, file_path, file_mtime, file_size, thumbnail_path FROM photos")
    existing_rows = cur.fetchall()
    existing = {r["file_path"]: r for r in existing_rows}

    seen_paths: set[str] = set()
    new_count = 0
    updated_count = 0
    indexed_count = 0
    error_count = 0
    batch_pending = 0

    def _flush_batch():
        nonlocal batch_pending
        if batch_pending > 0:
            conn.commit()
            _set_index_state(
                indexed=indexed_count,
                new=new_count,
                updated=updated_count,
                errors=error_count,
                message=f"Indexing... {indexed_count} processed",
            )
            batch_pending = 0

    for root, _dirs, files in os.walk(PHOTO_ROOT):
        for name in files:
            ext = Path(name).suffix.lower()
            if ext not in SUPPORTED_EXT:
                continue

            full = Path(root) / name
            path_str = str(full)
            seen_paths.add(path_str)

            try:
                stat = full.stat()
                file_mtime = stat.st_mtime
                file_size = stat.st_size
            except Exception:
                error_count += 1
                continue

            existing_row = existing.get(path_str)
            if not existing_row:
                try:
                    date_taken = get_date_taken(full)
                except Exception:
                    date_taken = datetime.fromtimestamp(file_mtime)

                cur.execute(
                    """
                    INSERT INTO photos (file_path, date_taken, thumbnail_path, file_mtime, file_size)
                    VALUES (?, ?, '', ?, ?)
                    """,
                    (path_str, date_taken.isoformat(), file_mtime, file_size),
                )
                new_count += 1
                indexed_count += 1
                batch_pending += 1
                if batch_pending >= INDEX_BATCH_SIZE:
                    _flush_batch()
                continue

            changed = force or (
                existing_row["file_mtime"] != file_mtime
                or existing_row["file_size"] != file_size
            )
            if changed:
                try:
                    date_taken = get_date_taken(full)
                except Exception:
                    date_taken = datetime.fromtimestamp(file_mtime)

                old_thumb = existing_row["thumbnail_path"]
                if old_thumb:
                    old_thumb_path = Path(old_thumb)
                    if old_thumb_path.exists():
                        try:
                            old_thumb_path.unlink()
                        except Exception:
                            pass

                cur.execute(
                    """
                    UPDATE photos
                    SET date_taken = ?, thumbnail_path = '', file_mtime = ?, file_size = ?
                    WHERE id = ?
                    """,
                    (date_taken.isoformat(), file_mtime, file_size, existing_row["id"]),
                )
                updated_count += 1
                indexed_count += 1
                batch_pending += 1
                if batch_pending >= INDEX_BATCH_SIZE:
                    _flush_batch()

    _flush_batch()

    missing_paths = [path for path in existing if path not in seen_paths]
    removed_count = 0
    if missing_paths:
        placeholders = ",".join(["?"] * len(missing_paths))
        cur.execute(
            f"SELECT id, thumbnail_path FROM photos WHERE file_path IN ({placeholders})",
            missing_paths,
        )
        rows = cur.fetchall()
        for row in rows:
            thumb_path = row["thumbnail_path"]
            if thumb_path:
                thumb = Path(thumb_path)
                if thumb.exists():
                    try:
                        thumb.unlink()
                    except Exception:
                        pass

        cur.execute(
            f"DELETE FROM selections WHERE photo_id IN (SELECT id FROM photos WHERE file_path IN ({placeholders}))",
            missing_paths,
        )
        cur.execute(f"DELETE FROM photos WHERE file_path IN ({placeholders})", missing_paths)
        removed_count = len(missing_paths)

    conn.commit()

    cur.execute("SELECT id, file_path FROM photos WHERE thumbnail_path = ''")
    rows = cur.fetchall()
    thumb_total = len(rows)
    thumb_done = 0
    for row in rows:
        file_path = Path(row["file_path"])
        if not file_path.exists():
            thumb_done += 1
            continue
        try:
            thumb = make_thumb(file_path, row["id"])
            cur.execute(
                "UPDATE photos SET thumbnail_path = ? WHERE id = ?",
                (str(thumb), row["id"]),
            )
        except Exception:
            error_count += 1
        thumb_done += 1
        if thumb_done % INDEX_BATCH_SIZE == 0:
            conn.commit()
            _set_index_state(
                message=f"Generating thumbnails... {thumb_done}/{thumb_total}",
            )

    conn.commit()
    conn.close()

    _set_index_state(
        state="complete",
        finished_at=datetime.utcnow().isoformat(),
        indexed=indexed_count,
        new=new_count,
        updated=updated_count,
        removed=removed_count,
        errors=error_count,
        message="Index complete",
    )


def _run_index_in_background(force: bool = False):
    try:
        index_photos(force=force)
    except Exception as exc:
        _set_index_state(
            state="error",
            finished_at=datetime.utcnow().isoformat(),
            message=f"Index failed: {exc}",
        )


def start_indexing(force: bool = False) -> bool:
    with _index_lock:
        if _index_state.get("state") == "running":
            return False

        _index_state.update(
            {
                "state": "running",
                "started_at": datetime.utcnow().isoformat(),
                "finished_at": None,
                "indexed": 0,
                "new": 0,
                "updated": 0,
                "removed": 0,
                "errors": 0,
                "message": "Indexing...",
            }
        )

    thread = threading.Thread(
        target=_run_index_in_background,
        kwargs={"force": force},
        daemon=True,
    )
    thread.start()
    return True


@app.on_event("startup")
def startup():
    init_db()
    load_books()
    if PHOTO_ROOT.exists():
        start_indexing()


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
    .top { display: flex; gap: 10px; align-items: center; flex-wrap: wrap; margin: 0 0 12px 0; }
    .pill { border: 1px solid #ddd; border-radius: 999px; padding: 6px 10px; font-size: 13px; }
    .warn { color: #9c5c00; border-color: #e6c98a; background: #fff9ec; }
    .grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(180px, 1fr)); gap: 8px; }
    .card { border: 1px solid #ddd; border-radius: 10px; padding: 10px; }
    .book { display: flex; justify-content: space-between; gap: 8px; }
    .done { color: #0a7; font-weight: 600; }
    .meta { color: #555; font-size: 13px; margin-top: 6px; }
    button { padding: 10px 14px; border-radius: 8px; border: 1px solid #ccc; background: #fff; cursor: pointer; }
  </style>
</head>
<body>
  <h1>Photo Book Curator</h1>
  <div class="top">
    <span id="health" class="pill">Loading status…</span>
    <span id="index" class="pill">Loading index…</span>
    <button onclick="triggerIndex()">Reindex</button>
  </div>
  <div id="warnings"></div>
  <div id="books" class="grid"></div>

  <script>
    async function loadStatus() {
      const [healthRes, indexRes] = await Promise.all([
        fetch('/api/health'),
        fetch('/api/index/status')
      ]);
      const health = await healthRes.json();
      const index = await indexRes.json();

      const healthEl = document.getElementById('health');
      healthEl.textContent = health.ready ? 'Ready' : 'Needs setup';
      healthEl.className = `pill ${health.ready ? '' : 'warn'}`;

      document.getElementById('index').textContent = `Index: ${index.state}`;

      const warningsEl = document.getElementById('warnings');
      if ((health.warnings || []).length) {
        warningsEl.innerHTML = health.warnings
          .map(w => `<div class="pill warn" style="margin-bottom:6px;display:inline-block;">${w}</div>`)
          .join('<br/>');
      } else {
        warningsEl.innerHTML = '';
      }
    }

    async function triggerIndex() {
      await fetch('/api/index?force=true', { method: 'POST' });
      loadStatus();
    }

    async function loadBooks() {
      const res = await fetch('/api/books');
      const books = await res.json();
      const el = document.getElementById('books');
      el.innerHTML = books.map(b => `
        <div class="card">
          <div class="book">
            <div>${b.child} – Month ${String(b.month).padStart(2,'0')}</div>
            <div class="${b.completed ? 'done' : ''}">${b.completed ? 'Done' : ''}</div>
          </div>
          <div>${b.start_date} → ${b.end_date}</div>
          <div class="meta">Selected: ${b.selected_count} · Photos: ${b.photo_count}</div>
          <div style="margin-top:8px;"><a href="/book/${b.id}">Open</a></div>
        </div>
      `).join('');
    }

    loadStatus();
    loadBooks();
    setInterval(loadStatus, 4000);
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
    .thumb {{ border: 2px solid transparent; border-radius: 8px; overflow: hidden; background: #fff; }}
    .thumb.selected {{ border-color: #4f81bd; }}
    .thumb button {{ all: unset; display: block; cursor: pointer; width: 100%; }}
    img {{ width: 100%; height: auto; display: block; }}
    .caption {{ font-size: 12px; color: #666; padding: 6px 6px 8px 6px; text-align: center; }}
    .bar {{ display: flex; gap: 10px; align-items: center; margin-bottom: 12px; flex-wrap: wrap; }}
    .bar-grow {{ flex: 1 1 auto; }}
    .muted {{ color: #666; font-size: 13px; }}
    button {{ padding: 10px 14px; border-radius: 8px; border: 1px solid #ccc; background: #fff; cursor: pointer; }}
  </style>
</head>
<body>
  <div class="bar">
    <a href="/">← Back</a>
    <span id="title" class="bar-grow"></span>
    <span id="count"></span>
    <label><input type="checkbox" id="selectedOnly" onchange="toggleSelectedOnly()"/> Selected only</label>
    <button onclick="selectAllVisible()">Select all visible</button>
    <button onclick="clearSelected()">Clear selected</button>
    <button onclick="exportZip()">Export selected ZIP</button>
    <button id="doneBtn" onclick="toggleDone()">Mark done</button>
  </div>

  <div id="meta" class="muted"></div>
  <div id="grid" class="grid"></div>
  <div style="margin-top:12px;"><button id="loadMore" onclick="loadMore()" style="display:none;">Load more</button></div>

  <script>
    const bookId = "{book_id}";
    const limit = 240;

    let loadedSelected = new Set();
    let selectedCount = 0;
    let offset = 0;
    let hasMore = false;
    let selectedOnly = false;
    let completed = false;

    function updateCount() {{
      document.getElementById('count').textContent = `Selected: ${{selectedCount}}`;
    }}

    function updateDoneButton() {{
      document.getElementById('doneBtn').textContent = completed ? 'Mark not done' : 'Mark done';
    }}

    async function updateSelection(ids, selectedFlag) {{
      if (!ids.length) return;
      await fetch(`/api/book/${{bookId}}/selection`, {{
        method: 'PUT',
        headers: {{ 'Content-Type': 'application/json' }},
        body: JSON.stringify({{ ids, selected: selectedFlag }})
      }});
    }}

    async function load(reset = true) {{
      if (reset) {{
        offset = 0;
        loadedSelected = new Set();
        document.getElementById('grid').innerHTML = '';
      }}

      const res = await fetch(`/api/book/${{bookId}}?offset=${{offset}}&limit=${{limit}}&selected_only=${{selectedOnly}}`);
      if (!res.ok) {{
        alert('Failed to load this book');
        return;
      }}

      const data = await res.json();
      document.getElementById('title').textContent = `${{data.child}} – Month ${{String(data.month).padStart(2,'0')}} (${{data.start_date}} → ${{data.end_date}})`;

      completed = !!data.completed;
      selectedCount = Number(data.selected_count || 0);
      updateDoneButton();
      updateCount();

      const shown = Math.min(offset + data.photos.length, data.total_photos);
      document.getElementById('meta').textContent = `Showing ${{shown}} of ${{data.total_photos}} photos`;

      const grid = document.getElementById('grid');
      grid.insertAdjacentHTML('beforeend', data.photos.map(p => `
        <div class="thumb ${{p.selected ? 'selected' : ''}}" data-id="${{p.id}}">
          <button type="button" onclick="togglePhoto(${{p.id}})">
            <img src="/thumbs/${{p.thumbnail}}" loading="lazy" />
          </button>
          <div class="caption">${{p.date_taken}}</div>
        </div>
      `).join(''));

      data.photos.filter(p => p.selected).forEach(p => loadedSelected.add(p.id));
      hasMore = !!data.has_more;
      offset += data.photos.length;
      document.getElementById('loadMore').style.display = hasMore ? 'inline-block' : 'none';
    }}

    async function togglePhoto(id) {{
      const isSelected = loadedSelected.has(id);
      if (isSelected) {{
        loadedSelected.delete(id);
        selectedCount = Math.max(0, selectedCount - 1);
      }} else {{
        loadedSelected.add(id);
        selectedCount += 1;
      }}

      const el = document.querySelector(`[data-id='${{id}}']`);
      if (el) el.classList.toggle('selected');
      updateCount();
      await updateSelection([id], !isSelected);
    }}

    async function selectAllVisible() {{
      const changed = [];
      document.querySelectorAll('.thumb').forEach(el => {{
        const id = Number(el.getAttribute('data-id'));
        if (!loadedSelected.has(id)) {{
          loadedSelected.add(id);
          changed.push(id);
          el.classList.add('selected');
        }}
      }});

      if (changed.length) {{
        selectedCount += changed.length;
        updateCount();
        await updateSelection(changed, true);
      }}
    }}

    async function clearSelected() {{
      await fetch(`/api/book/${{bookId}}/selection`, {{ method: 'DELETE' }});
      selectedCount = 0;
      loadedSelected = new Set();
      document.querySelectorAll('.thumb.selected').forEach(el => el.classList.remove('selected'));
      updateCount();
      if (selectedOnly) load(true);
    }}

    async function exportZip() {{
      const res = await fetch(`/api/export/${{bookId}}`, {{
        method: 'POST',
        headers: {{ 'Content-Type': 'application/json' }},
        body: JSON.stringify({{}})
      }});

      if (!res.ok) {{
        alert('No selected photos available for export.');
        return;
      }}

      const blob = await res.blob();
      const url = window.URL.createObjectURL(blob);
      const a = document.createElement('a');
      const cd = res.headers.get('Content-Disposition') || '';
      const match = cd.match(/filename="?([^\";]+)"?/);
      a.download = match ? match[1] : `book_${{bookId}}.zip`;
      a.href = url;
      a.click();
      window.URL.revokeObjectURL(url);
    }}

    async function toggleDone() {{
      const res = await fetch(`/api/book/${{bookId}}/completion`, {{
        method: 'PATCH',
        headers: {{ 'Content-Type': 'application/json' }},
        body: JSON.stringify({{ completed: !completed }})
      }});
      const data = await res.json();
      completed = !!data.completed;
      updateDoneButton();
    }}

    function toggleSelectedOnly() {{
      selectedOnly = document.getElementById('selectedOnly').checked;
      load(true);
    }}

    function loadMore() {{
      if (hasMore) load(false);
    }}

    load(true);
  </script>
</body>
</html>
        """
    )


@app.get("/api/books")
def api_books():
    conn = get_db()
    cur = conn.cursor()

    cur.execute(
        """
        SELECT
            b.*,
            (
                SELECT COUNT(*)
                FROM photos p
                WHERE date(p.date_taken) >= date(b.start_date)
                  AND date(p.date_taken) <= date(b.end_date)
            ) AS photo_count,
            (
                SELECT COUNT(*)
                FROM selections s
                WHERE s.book_id = b.id AND s.selected = 1
            ) AS selected_count
        FROM books b
        ORDER BY b.child, b.month
        """
    )
    rows = [dict(row) for row in cur.fetchall()]
    conn.close()
    return rows


def _get_book_or_404(cur: sqlite3.Cursor, book_id: str):
    cur.execute("SELECT * FROM books WHERE id = ?", (book_id,))
    book = cur.fetchone()
    if not book:
        raise HTTPException(status_code=404, detail="Book not found")
    return book


def _normalize_id_list(ids: list[Any]) -> list[int]:
    output: list[int] = []
    for value in ids:
        try:
            output.append(int(value))
        except Exception:
            continue
    return list(dict.fromkeys(output))


def _filter_ids_for_book(cur: sqlite3.Cursor, book: sqlite3.Row, ids: list[int]) -> list[int]:
    if not ids:
        return []
    placeholders = ",".join(["?"] * len(ids))
    cur.execute(
        f"""
        SELECT id
        FROM photos
        WHERE id IN ({placeholders})
          AND date(date_taken) >= date(?)
          AND date(date_taken) <= date(?)
        """,
        (*ids, book["start_date"], book["end_date"]),
    )
    return [int(row["id"]) for row in cur.fetchall()]


@app.get("/api/book/{book_id}")
def api_book(
    book_id: str,
    offset: int = Query(default=0, ge=0),
    limit: int = Query(default=240, ge=1, le=500),
    selected_only: bool = False,
):
    conn = get_db()
    cur = conn.cursor()

    book = _get_book_or_404(cur, book_id)
    selected_filter = "AND COALESCE(s.selected, 0) = 1" if selected_only else ""

    cur.execute(
        f"""
        SELECT COUNT(*) AS c
        FROM photos p
        LEFT JOIN selections s ON s.book_id = ? AND s.photo_id = p.id
        WHERE date(p.date_taken) >= date(?) AND date(p.date_taken) <= date(?)
        {selected_filter}
        """,
        (book_id, book["start_date"], book["end_date"]),
    )
    total_photos = int(cur.fetchone()["c"])

    cur.execute(
        f"""
        SELECT p.id, p.thumbnail_path, p.date_taken, COALESCE(s.selected, 0) AS selected
        FROM photos p
        LEFT JOIN selections s ON s.book_id = ? AND s.photo_id = p.id
        WHERE date(p.date_taken) >= date(?) AND date(p.date_taken) <= date(?)
        {selected_filter}
        ORDER BY p.date_taken ASC, p.id ASC
        LIMIT ? OFFSET ?
        """,
        (book_id, book["start_date"], book["end_date"], limit, offset),
    )
    photos = [
        {
            "id": row["id"],
            "thumbnail": Path(row["thumbnail_path"]).name,
            "selected": bool(row["selected"]),
            "date_taken": (row["date_taken"] or "")[:10],
        }
        for row in cur.fetchall()
        if row["thumbnail_path"]
    ]

    cur.execute(
        "SELECT COUNT(*) AS c FROM selections WHERE book_id = ? AND selected = 1",
        (book_id,),
    )
    selected_count = int(cur.fetchone()["c"])

    conn.close()

    return {
        **dict(book),
        "photos": photos,
        "selected_count": selected_count,
        "total_photos": total_photos,
        "offset": offset,
        "limit": limit,
        "has_more": (offset + len(photos)) < total_photos,
    }


@app.patch("/api/book/{book_id}/completion")
def set_book_completion(book_id: str, payload: dict):
    completed = bool(payload.get("completed"))

    conn = get_db()
    cur = conn.cursor()
    _get_book_or_404(cur, book_id)
    cur.execute("UPDATE books SET completed = ? WHERE id = ?", (1 if completed else 0, book_id))
    conn.commit()
    conn.close()

    return {"ok": True, "completed": completed}


@app.put("/api/book/{book_id}/selection")
def update_selection(book_id: str, payload: dict):
    ids = _normalize_id_list(payload.get("ids") or [])
    selected = bool(payload.get("selected", True))

    if not ids:
        return {"ok": True, "updated": 0}

    conn = get_db()
    cur = conn.cursor()
    book = _get_book_or_404(cur, book_id)
    valid_ids = _filter_ids_for_book(cur, book, ids)

    now = datetime.utcnow().isoformat()
    for photo_id in valid_ids:
        cur.execute(
            """
            INSERT INTO selections (book_id, photo_id, selected, updated_at)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(book_id, photo_id) DO UPDATE SET
                selected = excluded.selected,
                updated_at = excluded.updated_at
            """,
            (book_id, photo_id, 1 if selected else 0, now),
        )

    conn.commit()
    conn.close()

    return {"ok": True, "updated": len(valid_ids)}


@app.delete("/api/book/{book_id}/selection")
def clear_selection(book_id: str):
    conn = get_db()
    cur = conn.cursor()
    _get_book_or_404(cur, book_id)
    cur.execute("DELETE FROM selections WHERE book_id = ?", (book_id,))
    conn.commit()
    conn.close()
    return {"ok": True}


@app.post("/api/export/{book_id}")
def export_zip(book_id: str, payload: dict, background_tasks: BackgroundTasks):
    ids = _normalize_id_list(payload.get("ids") or [])

    conn = get_db()
    cur = conn.cursor()
    book = _get_book_or_404(cur, book_id)

    if ids:
        valid_ids = _filter_ids_for_book(cur, book, ids)
    else:
        cur.execute(
            "SELECT photo_id FROM selections WHERE book_id = ? AND selected = 1",
            (book_id,),
        )
        selected_ids = [int(row["photo_id"]) for row in cur.fetchall()]
        valid_ids = _filter_ids_for_book(cur, book, selected_ids)

    if not valid_ids:
        conn.close()
        raise HTTPException(status_code=400, detail="No selected photos")

    placeholders = ",".join(["?"] * len(valid_ids))
    cur.execute(f"SELECT file_path FROM photos WHERE id IN ({placeholders})", valid_ids)
    files = [row["file_path"] for row in cur.fetchall()]
    conn.close()

    existing_files = [path for path in files if Path(path).exists()]
    if not existing_files:
        raise HTTPException(status_code=400, detail="Selected files no longer exist")

    file_name = (
        f"{book['child']}_Month_{int(book['month']):02d}_{book['start_date']}_to_{book['end_date']}.zip"
    )

    with tempfile.NamedTemporaryFile(prefix="export_", suffix=".zip", dir=str(DATA_DIR), delete=False) as tmp:
        out_path = Path(tmp.name)

    with zipfile.ZipFile(out_path, "w", zipfile.ZIP_DEFLATED) as archive:
        for src in existing_files:
            archive.write(src, arcname=Path(src).name)

    background_tasks.add_task(lambda p: Path(p).unlink(missing_ok=True), str(out_path))
    return FileResponse(out_path, filename=file_name, media_type="application/zip")


@app.get("/api/index/status")
def index_status():
    return _get_index_state()


@app.post("/api/index")
def trigger_index(force: bool = False):
    started = start_indexing(force=force)
    return {"started": started, "status": _get_index_state()}


@app.get("/api/health")
def health():
    warnings = list(_book_load_warnings)
    if not PHOTO_ROOT.exists():
        warnings.append(f"PHOTO_ROOT does not exist: {PHOTO_ROOT}")
    if not BOOKS_PATH.exists():
        warnings.append(f"Books config missing: {BOOKS_PATH}")

    return {
        "ready": len(warnings) == 0,
        "warnings": warnings,
        "photo_root": str(PHOTO_ROOT),
        "books_path": str(BOOKS_PATH),
        "index": _get_index_state(),
    }
