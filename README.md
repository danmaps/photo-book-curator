# Photo Book Curator

Lightweight home-lab app to curate monthly photo books from a local photo mirror.

## Quick start

1. Copy example config:

```powershell
copy config\books.example.json config\books.json
```

1. Set env vars:

```powershell
PHOTO_ROOT=C:\path\to\photo\mirror
DATA_DIR=C:\Users\danny\.openclaw\workspace\photo-book-curator\data
```

1. Install and run:

```powershell
pip install -r requirements.txt
python -m uvicorn app.main:app --host 0.0.0.0 --port 3892
```

## Self-hosting

### Prerequisites

- **Python 3.11+**
- A folder of photos (JPG, JPEG, PNG, HEIC) — typically an iCloud Photos mirror or similar
- ~50 MB disk for thumbnails per 10,000 photos

### 1. Clone and configure

```bash
git clone <repo-url> photo-book-curator
cd photo-book-curator
cp config/books.example.json config/books.json
```

Edit `config/books.json` to define your books (name, start/end dates). See the example file for format.

### 2. Environment variables

| Variable | Required | Default | Description |
|---|---|---|---|
| `PHOTO_ROOT` | **yes** | — | Absolute path to your photo library folder |
| `DATA_DIR` | no | `./data` | Where the SQLite DB and thumbnails are stored |
| `BOOKS_PATH` | no | `./config/books.json` | Path to books config |
| `PORT` | no | `3892` | Server listen port |

```bash
# Linux / macOS
export PHOTO_ROOT=/mnt/photos/icloud
export DATA_DIR=/opt/photo-book-curator/data

# Windows PowerShell
$env:PHOTO_ROOT = "C:\Users\you\iCloudPhotos\Photos"
$env:DATA_DIR   = "C:\photo-book-curator\data"
```

### 3. Install dependencies and run

```bash
pip install -r requirements.txt
python -m uvicorn app.main:app --host 0.0.0.0 --port 3892
```

Open `http://<your-ip>:3892` in a browser. Indexing starts automatically on first launch.

### 4. Run as a background service

#### Linux (systemd)

Create `/etc/systemd/system/photo-book-curator.service`:

```ini
[Unit]
Description=Photo Book Curator
After=network.target

[Service]
Type=simple
User=youruser
WorkingDirectory=/opt/photo-book-curator
Environment=PHOTO_ROOT=/mnt/photos/icloud
Environment=DATA_DIR=/opt/photo-book-curator/data
ExecStart=/usr/bin/python3 -m uvicorn app.main:app --host 0.0.0.0 --port 3892
Restart=on-failure
RestartSec=5

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now photo-book-curator
```

#### Windows (Task Scheduler / NSSM)

Option A — **Task Scheduler**: Create a task that runs at logon:

```
Program: python
Arguments: -m uvicorn app.main:app --host 0.0.0.0 --port 3892
Start in: C:\path\to\photo-book-curator
```

Set the environment variables for the task under *Edit Trigger → Environment*.

Option B — **NSSM** (Non-Sucking Service Manager):

```powershell
nssm install PhotoBookCurator python -m uvicorn app.main:app --host 0.0.0.0 --port 3892
nssm set PhotoBookCurator AppDirectory C:\path\to\photo-book-curator
nssm set PhotoBookCurator AppEnvironmentExtra PHOTO_ROOT=C:\Photos DATA_DIR=C:\photo-book-curator\data
nssm start PhotoBookCurator
```

#### Docker (optional)

There is no Dockerfile shipped. A minimal one:

```dockerfile
FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
EXPOSE 3892
CMD ["python", "-m", "uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "3892"]
```

```bash
docker build -t photo-book-curator .
docker run -d \
  -p 3892:3892 \
  -v /mnt/photos:/photos:ro \
  -v ./data:/app/data \
  -e PHOTO_ROOT=/photos \
  photo-book-curator
```

### 5. Persistent data

All state lives in `DATA_DIR`:
- `photo_book.db` — SQLite database (photo index, selections, completion flags)
- `thumbs/` — generated thumbnails

Back up this folder to preserve your curation progress. The photo index can always be rebuilt from source photos, but selections and completion states are stored only in the database.

---

## Daily workflow

- Open the app home page and verify status pills show `Ready` and `Index: complete`
- Pick a book, select photos, refresh freely (selection persists in SQLite)
- Use `Selected only` to quickly review chosen images
- Export selected photos as ZIP
- Mark a book done or not-done as needed

## Operational notes

- Videos are ignored
- EXIF `DateTimeOriginal` is preferred, fallback is file modified time
- Thumbnails are stored under `DATA_DIR\thumbs`
- Indexing runs in a background thread on startup (non-blocking)
- Use the `Reindex` button on home page to force refresh

## API endpoints

- `GET /api/health` readiness and warnings
- `GET /api/index/status` index progress and last result
- `POST /api/index?force=true` trigger full reindex
- `GET /api/books` books with selected/photo counts
- `GET /api/book/{book_id}` paginated book photos (`offset`, `limit`, `selected_only`)
- `PUT /api/book/{book_id}/selection` persist select/deselect state
- `DELETE /api/book/{book_id}/selection` clear selection for a book
- `PATCH /api/book/{book_id}/completion` set done state
- `POST /api/export/{book_id}` export selected photos ZIP
