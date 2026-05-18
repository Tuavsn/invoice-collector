# GDT Invoice Crawler

**Production-grade automated invoice downloader for the Vietnam General Department of Taxation (GDT) portal.**

Built with Flask · Playwright Async · SQLite · SocketIO

---

## Features

- ✅ Invoice search by date range
- ✅ Downloads XML, PDF, and attachments per invoice
- ✅ Structured file storage (`invoices/YYYY/MM/DD/invoice_no/`)
- ✅ XML metadata parsing and SQLite persistence
- ✅ Real-time log streaming via SocketIO
- ✅ Excel VAT summary export (multi-sheet, accounting format)
- ✅ Flask dashboard with Chart.js visualizations
- ✅ Docker-ready

---

## Quick Start (Local)

### 1. Prerequisites

- Python 3.11+
- Chromium (installed via Playwright)

### 2. Clone and configure

```bash
git clone <repo>
cd invoice-crawler
cp .env.example .env
# Edit .env with your GDT credentials
nano .env
```

### 3. Install Python dependencies

```bash
pip install -r requirements.txt
```

### 4. Install Playwright browsers

```bash
playwright install chromium
# Or install all browsers:
playwright install
```

### 6. Run the application

```bash
python run.py
```

Open your browser at: **http://localhost:5000**

---

## Docker Setup

### Build and run

```bash
docker compose up --build -d
```

### View logs

```bash
docker compose logs -f invoice-crawler
```

### Stop

```bash
docker compose down
```

All data is persisted via Docker volumes (`invoices.db`, `invoices/`, `exports/`, `logs/`).

---

## Usage

### Start a Crawl

1. Navigate to **http://localhost:5000/crawler**
2. Enter your GDT credentials (username + password)
3. Set the date range (`dd/MM/yyyy` format)
4. Click **Start Crawl**
5. Watch live logs in the terminal panel

### View Invoices

- Go to **Invoices** in the sidebar
- Search by invoice number, seller, or buyer
- Filter by date range and type
- Click the eye icon for XML preview and file downloads

### Export Excel

1. Navigate to **Export Excel**
2. Set optional date filter
3. Click **Generate & Download Excel**
4. File is saved to `exports/` and downloaded automatically

---

## Project Structure

```
invoice-crawler/
├── app/
│   ├── main.py              # Flask app factory
│   ├── config.py            # All configuration (env-based)
│   ├── extensions.py        # Flask extension instances
│   ├── automation/
│   │   ├── browser.py       # Playwright browser lifecycle
│   │   ├── captcha.py       # 
│   │   ├── login.py         # GDT portal login automation
│   │   ├── invoice_search.py# Search page automation
│   │   ├── invoice_export.py# Invoice list export
│   │   ├── invoice_detail.py# Per-row XML/PDF download
│   │   └── crawler_engine.py# Orchestration engine
│   ├── db/
│   │   ├── models.py        # SQLAlchemy models
│   │   ├── repository.py    # Data access layer
│   │   └── database.py      # DB initialisation
│   ├── services/
│   │   ├── crawler_service.py  # Async/sync bridge
│   │   ├── invoice_service.py  # Invoice business logic
│   │   ├── xml_service.py      # XML parsing
│   │   ├── excel_service.py    # Excel report generation
│   │   └── file_service.py     # File management
│   ├── routes/              # Flask blueprints
│   ├── templates/           # Jinja2 HTML templates
│   └── utils/               # Helpers, retry, logging
├── downloads/               # Raw portal downloads
├── invoices/                # Structured invoice files
├── exports/                 # Generated Excel reports
├── logs/                    # Application logs + screenshots
├── Dockerfile
├── docker-compose.yml
├── requirements.txt
├── run.py                   # Entry point
└── .env.example
```

---

## Environment Variables

| Variable | Default | Description |
|---|---|---|
| `GDT_USERNAME` | — | **Required.** Portal login username |
| `GDT_PASSWORD` | — | **Required.** Portal login password |
| `FLASK_SECRET_KEY` | `dev-secret…` | Change in production |
| `FLASK_ENV` | `production` | `development` enables debug |
| `FLASK_PORT` | `5000` | HTTP port |
| `DATABASE_URL` | `sqlite:///invoices.db` | SQLAlchemy URL |
| `PLAYWRIGHT_HEADLESS` | `true` | `false` to see the browser |
| `PLAYWRIGHT_TIMEOUT` | `30000` | ms for Playwright waits |
| `CRAWLER_MAX_RETRIES` | `5` | Login/download retry count |
| `CRAWLER_PAGE_SIZE` | `50` | Rows per search page |
| `CRAWLER_DELAY_MS` | `500` | Delay between invoice rows |

---

---

## Database

### Schema

**invoices** — one row per downloaded invoice  
**crawl_jobs** — one row per crawl session  
**app_settings** — key-value runtime configuration

### Backup

```bash
cp invoices.db invoices_backup_$(date +%Y%m%d).db
```

### Migrate to PostgreSQL

1. Change `DATABASE_URL` in `.env`:
   ```
   DATABASE_URL=postgresql://user:pass@host:5432/invoices
   ```
2. Install psycopg2: `pip install psycopg2-binary`
3. Restart — SQLAlchemy creates the schema automatically.

---

## API Endpoints

| Method | Path | Description |
|---|---|---|
| `POST` | `/crawler/api/start` | Start a crawl job |
| `POST` | `/crawler/api/stop` | Stop active crawl |
| `GET` | `/crawler/api/status` | Crawl status + recent jobs |
| `GET` | `/invoices/api/list` | Paginated invoice JSON |
| `POST` | `/export/api/excel` | Generate Excel report |
| `GET` | `/settings/api/all` | All settings as JSON |
| `POST` | `/settings/api/save` | Update settings |

---

## Security Notes

- **Never** commit `.env` to version control
- Change `FLASK_SECRET_KEY` in production
- The application runs behind a trusted internal network — do not expose it to the public internet
- GDT credentials are passed per-request from the UI and are not stored in the database

---

## License

Internal use only. All rights reserved.