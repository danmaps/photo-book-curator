# Photo Book Curator

Lightweight home-lab app to curate monthly photo books from a local photo mirror.

## Quick start
1) Copy example config:
```
copy config\books.example.json config\books.json
```
2) Set env vars:
```
PHOTO_ROOT=C:\path\to\photo\mirror
DATA_DIR=C:\Users\danny\.openclaw\workspace\photo-book-curator\data
```
3) Install and run:
```
pip install -r requirements.txt
python -m uvicorn app.main:app --host 0.0.0.0 --port 3892
```

## Notes
- Videos are ignored
- EXIF DateTimeOriginal preferred, fallback to file mtime
- Thumbnails stored under DATA_DIR\thumbs
