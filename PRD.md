# Photo Book Curator PRD v1.0

## Overview
Photo Book Curator is a lightweight home-lab web app that helps parents curate monthly photo books for their children’s first year of life. The app presents photos book by book, filtering by predefined monthly date ranges, excluding videos, and enabling fast selection and export of images for upload to iCloud Shared Albums or photo book services. The product intentionally avoids fragile direct integrations with iCloud Photos APIs, instead operating on a local mirror of the photo library for reliability and speed.

## Goals
- Make it fast and calm to curate photos month by month
- Eliminate scrolling through irrelevant dates or videos
- Provide clear “this book is done” progress
- Keep the system reliable and low-maintenance
- Ship something usable quickly, not perfect

## Non-Goals
- Direct, official integration with iCloud Photos APIs
- Automatic uploads to iCloud Shared Albums (Phase 1)
- Complex photo editing or layout tools
- Multi-user or public access
- Cloud hosting or mobile apps

## Users
- Primary: Parent curating monthly photo books for their children
- Environment: Home lab, private LAN
- Devices: Desktop browser, iPad, iPhone (Safari/Chrome)

## Core Concepts
### Book
A Book represents one month of a child’s life.
- Child name (e.g., Rowan, Willow)
- Month number (1–12)
- Start date (inclusive)
- End date (inclusive)
- Completion status

### Photo
A Photo is an image file with:
- Capture date (EXIF DateTimeOriginal preferred)
- File path
- Thumbnail
- Media type (image only; videos excluded)

## Functional Requirements
### 1. Photo Ingestion
- Photos are sourced from a local mirrored folder (e.g., icloudpd output).
- Videos are ignored.
- Metadata extraction rules:
  1. Use EXIF DateTimeOriginal if present
  2. Fallback to file modified time
- Supported image formats:
  - JPEG, HEIC, PNG (extensible)

### 2. Book Definitions
- Books are preconfigured via a static config file or database seed.
- Each book includes:
  - child
  - month number
  - start date
  - end date
- Example: Willow – Month 8 2026-01-23 → 2026-02-22

### 3. Book View
- User selects a book.
- App displays a grid of thumbnails:
  - Only photos within the date range
  - Only images (no videos)
  - Fast scrolling and selection
- Selection state is visible and persistent during session

### 4. Selection Tools
- Click to select / deselect photos
- “Select all visible”
- Selected count indicator

### 5. Export
- User can export selected photos as:
  - ZIP file (default)
- Export naming format: {Child}_Month_{NN}_{start}_to_{end}.zip
- Export contains original files, not thumbnails.

### 6. Completion Tracking
- User can mark a book as “Done”
- Completion state stored in SQLite
- Completed books are visually distinguished

## Non-Functional Requirements
### Performance
- Thumbnail grid should load quickly on LAN
- Scrolling should feel smooth on iPad

### Reliability
- App must function without internet access
- No dependency on live iCloud APIs

### Security
- App runs on private network only
- No external auth required (Phase 1)

## UX Requirements
### Navigation
- Home screen lists:
  - Children
  - Months 1–12 per child
  - Completion status

### Book Screen
- Header:
  - Book name
  - Date range
  - Selected count
- Main:
  - Thumbnail grid
- Footer:
  - Export button
  - Mark done button

### Visual Tone
- Calm, minimal, no clutter
- Focus on photos, not controls

## Technical Architecture
### Backend
- Python + FastAPI
- SQLite database
- Background indexer:
  - scans photo directory
  - extracts metadata
  - generates thumbnails

### Frontend
- Simple SPA or server-rendered pages
- Grid layout optimized for touch
- Minimal JS, no heavy framework required

### Storage
- Photo originals remain in source directory
- Thumbnails stored in app data directory
- Database tracks:
  - photos
  - books
  - completion state

## Data Model (Simplified)
### Photo
- id
- file_path
- date_taken
- is_video (always false in v1)
- thumbnail_path

### Book
- id
- child
- month
- start_date
- end_date
- completed

## Deployment Environment
- Home lab
- Docker-based deployment preferred

### Services
- App container
- Persistent volume for:
  - thumbnails
  - database

## Phase Breakdown
### Phase 1 (MVP)
- Local photo indexing
- Book-based filtering
- Thumbnail grid
- Selection + ZIP export
- Completion tracking

### Phase 2
- Favorites filter
- Duplicate detection
- Keyboard shortcuts
- “Open iCloud Shared Album” helper link

### Phase 3 (Optional)
- Browser extension to assist iCloud.com uploads
- Automation experiments (best-effort)

## Open Questions
- Preferred source folder structure?
- HEIC handling on all target devices?
- Max library size expected?

## Success Criteria
- Monthly book selection takes minutes, not hours
- No need to scroll outside date range
- Books get finished consistently
- App stays boring and reliable
