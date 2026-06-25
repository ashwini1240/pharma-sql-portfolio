import os
import subprocess
import logging
import gzip
import shutil
from datetime import datetime, timedelta, timezone
from pathlib import Path

from dotenv import load_dotenv
from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload

# ── Load environment ─────────────────────────────────────────────────────────

load_dotenv(r"D:\Projects\GoogleDrive\.env")

PG_HOST      = os.getenv("POSTGRES_HOST", "localhost")
PG_PORT      = os.getenv("POSTGRES_PORT", "5432")
PG_USER      = os.getenv("POSTGRES_USER", "postgres")
PG_PASSWORD  = os.getenv("POSTGRES_PASSWORD", "")
PG_DB        = os.getenv("POSTGRES_DB", "postgres")
PG_BIN       = r"D:\PostgreSQL\17\bin"

BACKUP_DIR   = Path(os.getenv("BACKUP_DIR",  r"D:\Backups\PostgreSQL"))
POWERBI_DIR  = Path(os.getenv("POWERBI_DIR", r"D:\Projects\PowerBI"))
CREDS_FILE   = os.getenv("GDRIVE_CREDENTIALS", r"D:\Projects\GoogleDrive\credentials.json")
TOKEN_FILE   = os.getenv("GDRIVE_TOKEN",       r"D:\Projects\GoogleDrive\token.json")
LOG_FILE     = Path(os.getenv("LOG_FILE",      r"D:\Backups\backup_log.txt"))

GDRIVE_FOLDER  = "Laptop Backups"
RETENTION_DAYS = 7
SCOPES         = ["https://www.googleapis.com/auth/drive"]

# ── Logging ──────────────────────────────────────────────────────────────────

LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
BACKUP_DIR.mkdir(parents=True, exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    handlers=[
        logging.FileHandler(LOG_FILE, encoding="utf-8"),
        logging.StreamHandler(),
    ],
)
log = logging.getLogger(__name__)

# ── Google Drive ─────────────────────────────────────────────────────────────

def get_drive_service():
    creds = None
    if Path(TOKEN_FILE).exists():
        creds = Credentials.from_authorized_user_file(TOKEN_FILE, SCOPES)
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            flow = InstalledAppFlow.from_client_secrets_file(CREDS_FILE, SCOPES)
            creds = flow.run_local_server(port=0)
        Path(TOKEN_FILE).write_text(creds.to_json(), encoding="utf-8")
    return build("drive", "v3", credentials=creds)


def get_or_create_folder(service, name: str, parent_id: str = None) -> str:
    q = (
        f"name='{name}' and mimeType='application/vnd.google-apps.folder'"
        " and trashed=false"
    )
    if parent_id:
        q += f" and '{parent_id}' in parents"
    results = service.files().list(q=q, fields="files(id)").execute()
    items = results.get("files", [])
    if items:
        return items[0]["id"]
    meta = {"name": name, "mimeType": "application/vnd.google-apps.folder"}
    if parent_id:
        meta["parents"] = [parent_id]
    f = service.files().create(body=meta, fields="id").execute()
    log.info("Created Drive folder: %s", name)
    return f["id"]


def upload_file(service, path: Path, folder_id: str):
    media = MediaFileUpload(str(path), resumable=True)
    meta  = {"name": path.name, "parents": [folder_id]}
    f = service.files().create(body=meta, media_body=media, fields="id").execute()
    log.info("Uploaded %-45s  id=%s", path.name, f["id"])


def delete_old_drive_files(service, folder_id: str):
    cutoff = (datetime.now(timezone.utc) - timedelta(days=RETENTION_DAYS)).strftime("%Y-%m-%dT%H:%M:%SZ")
    q = f"'{folder_id}' in parents and trashed=false and createdTime < '{cutoff}'"
    results = service.files().list(q=q, fields="files(id,name,createdTime)").execute()
    for f in results.get("files", []):
        service.files().delete(fileId=f["id"]).execute()
        log.info("Removed old Drive file: %s (created %s)", f["name"], f["createdTime"])

# ── PostgreSQL ────────────────────────────────────────────────────────────────

def list_databases() -> list[str]:
    psql = str(Path(PG_BIN) / "psql.exe")
    env  = {**os.environ, "PGPASSWORD": PG_PASSWORD}
    out  = subprocess.run(
        [psql, "-h", PG_HOST, "-p", PG_PORT, "-U", PG_USER, "-d", PG_DB,
         "-t", "-c", "SELECT datname FROM pg_database WHERE datistemplate = false;"],
        capture_output=True, text=True, env=env, check=True,
    )
    return [db.strip() for db in out.stdout.splitlines() if db.strip()]


def dump_database(db: str, ts: str) -> Path | None:
    pg_dump = str(Path(PG_BIN) / "pg_dump.exe")
    raw_out = BACKUP_DIR / f"{db}_{ts}.sql"
    gz_out  = BACKUP_DIR / f"{db}_{ts}.sql.gz"
    env     = {**os.environ, "PGPASSWORD": PG_PASSWORD}
    try:
        result = subprocess.run(
            [pg_dump, "-h", PG_HOST, "-p", PG_PORT, "-U", PG_USER, db],
            capture_output=True, env=env,
        )
        if result.returncode != 0:
            log.error("pg_dump error for %s: %s", db, result.stderr.decode())
            return None
        with open(raw_out, "wb") as f:
            f.write(result.stdout)
        with open(raw_out, "rb") as f_in, gzip.open(gz_out, "wb") as f_out:
            shutil.copyfileobj(f_in, f_out)
        raw_out.unlink()
        log.info("Dumped %s → %s  (%.1f MB)", db, gz_out.name,
                 gz_out.stat().st_size / 1_048_576)
        return gz_out
    except Exception as exc:
        log.exception("Failed to dump %s: %s", db, exc)
        return None


def purge_old_local_backups():
    cutoff = datetime.now() - timedelta(days=RETENTION_DAYS)
    for f in BACKUP_DIR.glob("*.sql.gz"):
        if datetime.fromtimestamp(f.stat().st_mtime) < cutoff:
            f.unlink()
            log.info("Deleted old local backup: %s", f.name)

# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    log.info("=" * 60)
    log.info("Backup started  %s", datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")

    # Google Drive
    try:
        svc           = get_drive_service()
        root_id       = get_or_create_folder(svc, GDRIVE_FOLDER)
        pg_folder_id  = get_or_create_folder(svc, "PostgreSQL", root_id)
        pbi_folder_id = get_or_create_folder(svc, "PowerBI",    root_id)
    except Exception as exc:
        log.exception("Google Drive auth failed: %s", exc)
        return

    # ── PostgreSQL backup ────────────────────────────────────────────────────
    log.info("--- PostgreSQL ---")
    try:
        databases = list_databases()
        log.info("Databases: %s", databases)
    except Exception as exc:
        log.exception("Cannot list databases: %s", exc)
        databases = []

    ok_pg = 0
    for db in databases:
        dump = dump_database(db, ts)
        if dump:
            try:
                upload_file(svc, dump, pg_folder_id)
                ok_pg += 1
            except Exception as exc:
                log.error("Upload failed for %s: %s", dump.name, exc)

    log.info("PostgreSQL: %d/%d databases uploaded", ok_pg, len(databases))

    # ── Power BI backup ──────────────────────────────────────────────────────
    log.info("--- Power BI ---")
    pbix_files = list(POWERBI_DIR.rglob("*.pbix")) if POWERBI_DIR.exists() else []
    log.info("Found %d .pbix file(s)", len(pbix_files))

    ok_pbi = 0
    for pbix in pbix_files:
        try:
            upload_file(svc, pbix, pbi_folder_id)
            ok_pbi += 1
        except Exception as exc:
            log.error("Upload failed for %s: %s", pbix.name, exc)

    log.info("PowerBI: %d/%d files uploaded", ok_pbi, len(pbix_files))

    # ── Retention cleanup ────────────────────────────────────────────────────
    log.info("--- Cleanup (last %d days) ---", RETENTION_DAYS)
    purge_old_local_backups()
    delete_old_drive_files(svc, pg_folder_id)
    delete_old_drive_files(svc, pbi_folder_id)

    log.info("Backup finished")
    log.info("=" * 60)


if __name__ == "__main__":
    main()
