from __future__ import annotations

import argparse
import imaplib
import json
import logging
import re
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import datetime, timedelta
from email import policy
from email.generator import BytesGenerator
from email.parser import BytesParser
from email.utils import parsedate_to_datetime
from io import BytesIO
from logging.handlers import TimedRotatingFileHandler
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import boto3
import requests
import yaml
from botocore.client import Config
from requests import RequestException


@dataclass
class Account:
    email: str
    username: str
    password: str
    imap_host: str
    imap_port: int


class BackupError(RuntimeError):
    """Base class for predictable backup errors with actionable context."""


class VaultUnavailableError(BackupError):
    """Vault is unreachable or returned an unexpected transport error."""


class VaultAccountSchemaError(BackupError):
    """Vault account secret has invalid structure."""


class ImapFetchError(BackupError):
    """IMAP server returned unexpected payload for a message fetch."""


class MessageParseError(BackupError):
    """Unable to parse raw message bytes."""


class MessageSerializeError(BackupError):
    """Unable to serialize message to RFC822 bytes."""


class LiveProgress:
    def __init__(self, enabled: bool):
        self.enabled = enabled and sys.stdout.isatty()
        self.lock = threading.Lock()
        self.accounts: dict[str, dict[str, Any]] = {}
        self.stop_event = threading.Event()
        self.thread: threading.Thread | None = None

    def start(self, account_keys: list[str]) -> None:
        if not self.enabled:
            return
        with self.lock:
            self.accounts = {
                key: {
                    "status": "queued",
                    "folders_done": 0,
                    "folders_total": 0,
                    "emails_done": 0,
                    "emails_total": 0,
                    "current_folder": "-",
                    "updated_at": time.time(),
                }
                for key in account_keys
            }
        self.thread = threading.Thread(target=self._render_loop, daemon=True, name="progress")
        self.thread.start()

    def stop(self) -> None:
        if not self.enabled:
            return
        self.stop_event.set()
        if self.thread:
            self.thread.join(timeout=2)
        self._render(final=True)
        print()

    def account_started(self, account_email: str, folders_total: int) -> None:
        self._update(
            account_email,
            status="running",
            folders_total=folders_total,
            current_folder="-",
        )

    def folder_started(self, account_email: str, folder: str, emails_total: int) -> None:
        with self.lock:
            data = self.accounts[account_email]
            data["current_folder"] = folder
            data["emails_total"] += emails_total
            data["updated_at"] = time.time()

    def uid_done(self, account_email: str) -> None:
        with self.lock:
            data = self.accounts[account_email]
            data["emails_done"] += 1
            data["updated_at"] = time.time()

    def folder_done(self, account_email: str) -> None:
        with self.lock:
            data = self.accounts[account_email]
            data["folders_done"] += 1
            data["current_folder"] = "-"
            data["updated_at"] = time.time()

    def account_done(self, account_email: str) -> None:
        self._update(account_email, status="done", current_folder="-")

    def account_failed(self, account_email: str) -> None:
        self._update(account_email, status="failed", current_folder="-")

    def _update(self, account_email: str, **kwargs: Any) -> None:
        with self.lock:
            data = self.accounts.setdefault(account_email, {})
            data.update(kwargs)
            data["updated_at"] = time.time()

    def _render_loop(self) -> None:
        while not self.stop_event.wait(0.4):
            self._render(final=False)

    def _render(self, final: bool) -> None:
        with self.lock:
            lines = [
                "📬 Live backup progress",
                "─" * 102,
                f"{'Email':40} {'Status':10} {'Folders':12} {'Emails':14} {'Current folder'}",
                "─" * 102,
            ]
            for email, item in sorted(self.accounts.items()):
                status = item.get("status", "queued")
                status_icon = {
                    "queued": "🕒 queued",
                    "running": "🔄 running",
                    "done": "✅ done",
                    "failed": "❌ failed",
                }.get(status, status)
                folders = f"{item.get('folders_done', 0)}/{item.get('folders_total', 0)}"
                emails = f"{item.get('emails_done', 0)}/{item.get('emails_total', 0)}"
                current = str(item.get("current_folder", "-"))[:30]
                lines.append(f"{email[:40]:40} {status_icon:10} {folders:12} {emails:14} {current}")
            lines.append("─" * 102)
            out = "\n".join(lines)

        if final:
            sys.stdout.write("\033[2J\033[H")
            sys.stdout.write(out)
            sys.stdout.flush()
            return
        sys.stdout.write("\033[2J\033[H")
        sys.stdout.write(out)
        sys.stdout.flush()


class VaultClient:
    def __init__(self, cfg: dict[str, Any]):
        self.addr = cfg["addr"].rstrip("/")
        self.role = cfg.get("jwt_role")
        self.base_path = cfg["accounts_base_path"].strip("/")
        self.mount = cfg.get("mount", "jwt")
        self.auth_method = cfg.get("auth_method", "jwt").lower()
        self.list_method = cfg.get("list_method", "list").lower()
        self.list_override_header = bool(cfg.get("list_override_header", False))
        self.token: str | None = None

    def _jwt_from_env(self) -> str:
        import os

        jwt = os.getenv("CI_JOB_JWT_V2") or os.getenv("CI_JOB_JWT")
        if not jwt:
            raise RuntimeError("GitLab JWT not found in CI_JOB_JWT_V2/CI_JOB_JWT")
        return jwt

    def authenticate(self) -> None:
        if self.auth_method == "token":
            import os

            token = os.getenv("VAULT_TOKEN")
            if not token:
                raise RuntimeError("Vault token auth selected, but VAULT_TOKEN is missing")
            self.token = token
            return

        jwt = self._jwt_from_env()
        if not self.role:
            raise RuntimeError("Vault jwt auth selected, but jwt_role is missing in config")
        url = f"{self.addr}/v1/auth/{self.mount}/login"
        try:
            r = requests.post(url, json={"role": self.role, "jwt": jwt}, timeout=20)
            r.raise_for_status()
            self.token = r.json()["auth"]["client_token"]
        except RequestException as exc:
            raise VaultUnavailableError(f"Vault auth failed for {self.addr}: {exc}") from exc

    def _headers(self) -> dict[str, str]:
        if not self.token:
            raise RuntimeError("Vault token is missing")
        return {"X-Vault-Token": self.token}

    def list_accounts(self) -> list[str]:
        # KV v2 LIST uses metadata endpoint.
        metadata_path = self.base_path.replace("/data/", "/metadata/", 1)
        url = f"{self.addr}/v1/{metadata_path}?list=true"
        headers = self._headers()
        try:
            if self.list_method == "get":
                if self.list_override_header:
                    headers["X-HTTP-Method-Override"] = "LIST"
                r = requests.get(url, headers=headers, timeout=20)
            else:
                r = requests.request("LIST", url, headers=headers, timeout=20)
            r.raise_for_status()
        except RequestException as exc:
            raise VaultUnavailableError(f"Failed to list accounts from Vault: {exc}") from exc
        keys = r.json().get("data", {}).get("keys", [])
        return [k.rstrip("/") for k in keys]

    def read_account(self, account_key: str) -> Account:
        url = f"{self.addr}/v1/{self.base_path}/{account_key}"
        try:
            r = requests.get(url, headers=self._headers(), timeout=20)
            r.raise_for_status()
        except RequestException as exc:
            raise VaultUnavailableError(
                f"Failed to load account '{account_key}' from Vault at {self.addr}: {exc}"
            ) from exc
        data = r.json()["data"]["data"]
        password = self._extract_password(account_key, data)
        return Account(
            email=account_key,
            username=data.get("username", account_key),
            password=password,
            imap_host=data.get("imap_host", "imap.yandex.ru"),
            imap_port=int(data.get("imap_port", 993)),
        )

    @staticmethod
    def _extract_password(account_key: str, data: dict[str, Any]) -> str:
        value = data.get("password")
        if not value and "passsword" in data:
            value = data.get("passsword")
        if value:
            return str(value)
        available = ", ".join(sorted(data.keys())) if data else "<empty>"
        raise VaultAccountSchemaError(
            f"Account '{account_key}' is missing required Vault field 'password'. "
            f"Available keys: {available}"
        )


class BackupRunner:
    def __init__(self, cfg: dict[str, Any], dry_run: bool = False, live_progress: bool = False):
        self.cfg = cfg
        self.tz = ZoneInfo(cfg["timezone"])
        self.now = datetime.now(tz=self.tz)
        self.dry_run = dry_run or bool(cfg.get("dry_run", False))
        self.local_root = Path(cfg["storage"]["local_root"])
        try:
            self.local_root.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            fallback = Path.cwd() / ".mail_backuper_dry_run"
            fallback.mkdir(parents=True, exist_ok=True)
            print(
                f"Dry-run local_root fallback to {fallback} "
                f"(could not use {self.local_root}: {exc})",
                file=sys.stderr,
            )
            self.local_root = fallback
           
        self.skip_folders = {x.lower() for x in cfg["imap"].get("skip_folders", ["Spam"])}
        self.retry_attempts = int(cfg.get("retry_attempts", 3))
        self.progress = LiveProgress(enabled=live_progress)

        s3_cfg = cfg["s3"]
        self.s3_bucket = s3_cfg["bucket"]
        self.s3_prefix = s3_cfg["prefix"].strip("/")
        s3_addressing_style = s3_cfg.get("addressing_style", "auto")
        s3_client_config = Config(
            signature_version=s3_cfg.get("signature_version", "s3v4"),
            s3={"addressing_style": s3_addressing_style},
        )
        self.s3_put_extra_args = self._build_s3_put_extra_args(s3_cfg)
        self.s3 = boto3.client(
            "s3",
            endpoint_url=s3_cfg["endpoint_url"],
            region_name=s3_cfg.get("region"),
            aws_access_key_id=s3_cfg.get("access_key_id"),
            aws_secret_access_key=s3_cfg.get("secret_access_key"),
            aws_session_token=s3_cfg.get("session_token"),
            verify=s3_cfg.get("verify_ssl", True),
            config=s3_client_config,
        )

    def run(self) -> None:
        vault = VaultClient(self.cfg["vault"])
        vault.authenticate()
        keys = vault.list_accounts()
        logging.info("Loaded %d accounts from Vault", len(keys))
        self.progress.start(keys)

        concurrency = int(self.cfg.get("concurrency", 4))
        continue_on_error = bool(self.cfg.get("limits", {}).get("continue_on_account_error", True))

        try:
            with ThreadPoolExecutor(max_workers=concurrency) as pool:
                futures = {}
                for key in keys:
                    try:
                        account = vault.read_account(key)
                    except Exception as exc:
                        self.progress.account_failed(key)
                        logging.exception("Failed to load account %s from Vault: %s", key, exc)
                        if not continue_on_error:
                            raise
                        continue
                    futures[pool.submit(self.backup_one_account, account)] = key
                for f in as_completed(futures):
                    key = futures[f]
                    try:
                        f.result()
                    except Exception as exc:
                        self.progress.account_failed(key)
                        logging.exception("Account %s failed: %s", key, exc)
                        if not continue_on_error:
                            raise
        finally:
            self.progress.stop()

    def backup_one_account(self, account: Account) -> None:
        logging.info("Start backup for %s", account.email)
        state_path = self.local_root / "_state" / f"{account.email}.json"
        state_path.parent.mkdir(parents=True, exist_ok=True)
        state = self._read_json(state_path)

        with imaplib.IMAP4_SSL(account.imap_host, account.imap_port) as imap:
            imap.login(account.username, account.password)
            status, folders = imap.list()
            if status != "OK":
                raise RuntimeError(f"Cannot list folders for {account.email}")
            folder_names = [
                self._extract_folder_name(folder_raw.decode("utf-8", errors="ignore"))
                for folder_raw in folders
            ]
            filtered_folders = [
                folder_name
                for folder_name in folder_names
                if folder_name and folder_name.lower() not in self.skip_folders
            ]
            self.progress.account_started(account.email, folders_total=len(filtered_folders))
            for folder_name in filtered_folders:
                self._backup_folder(account, imap, folder_name, state, state_path)

        if not self.dry_run:
            self._write_json_atomic(state_path, state)
        self.progress.account_done(account.email)
        logging.info("Done backup for %s", account.email)

    def _backup_folder(
        self,
        account: Account,
        imap: imaplib.IMAP4_SSL,
        folder: str,
        state: dict[str, Any],
        state_path: Path,
    ) -> None:
        folder_state = state.setdefault("folders", {}).setdefault(folder, {"last_uid": 0})
        last_uid = int(folder_state.get("last_uid", 0))

        status, _ = imap.select(f'"{folder}"', readonly=True)
        if status != "OK":
            logging.warning("Skip folder %s for %s", folder, account.email)
            return

        status, data = imap.uid("SEARCH", None, f"UID {last_uid + 1}:*")
        if status != "OK":
            logging.warning("Cannot search UIDs in %s", folder)
            return

        uid_list = data[0].split() if data and data[0] else []
        self.progress.folder_started(account.email, folder, emails_total=len(uid_list))
        if not uid_list:
            self.progress.folder_done(account.email)
            return

        max_uid = last_uid
        for uid_b in uid_list:
            uid = int(uid_b.decode())
            try:
                self._backup_uid(account, imap, folder, uid)
            except BackupError as exc:
                logging.warning("%s", exc)
                continue
            except Exception as exc:
                logging.exception(
                    "Unexpected error for %s folder=%s uid=%s: %s",
                    account.email,
                    folder,
                    uid,
                    exc,
                )
                continue
            self.progress.uid_done(account.email)
            max_uid = max(max_uid, uid)
            folder_state["last_uid"] = max_uid
            if not self.dry_run:
                self._write_json_atomic(state_path, state)

        folder_state["last_uid"] = max_uid
        self.progress.folder_done(account.email)

    def _backup_uid(self, account: Account, imap: imaplib.IMAP4_SSL, folder: str, uid: int) -> None:
        status, data = imap.uid("FETCH", str(uid), "(RFC822 INTERNALDATE)")
        if status != "OK" or not data or data[0] is None:
            return

        item = data[0]
        if not isinstance(item, tuple) or len(item) < 2 or not isinstance(item[1], (bytes, bytearray)):
            raise ImapFetchError(
                f"IMAP FETCH returned unexpected payload type for {account.email} {folder} UID {uid}: "
                f"{type(item).__name__}"
            )

        raw = bytes(item[1])
        try:
            msg = BytesParser(policy=policy.default).parsebytes(raw)
        except Exception as exc:
            raise MessageParseError(
                f"Failed to parse message for {account.email} {folder} UID {uid}: {exc}"
            ) from exc
        stripped = self._strip_attachments(msg)
        if stripped is None:
            return

        day = self._mail_date(stripped).astimezone(self.tz)
        day_path = day.strftime("%Y/%m/%d")
        folder_sane = folder.replace("/", "_")

        # local_dir = self.local_root / account.email / day_path / folder_sane
        # local_dir.mkdir(parents=True, exist_ok=True)
        # local_file = local_dir / f"{uid}.eml"

        eml_bytes = self._to_bytes(stripped, account.email, folder, uid)

        # if not self.dry_run:
        #     local_file.write_bytes(eml_bytes)

        s3_key = f"{self.s3_prefix}/{account.email}/{day_path}/{folder_sane}/{uid}.eml"
        if not self.dry_run:
            self.s3.put_object(
                Bucket=self.s3_bucket,
                Key=s3_key,
                Body=eml_bytes,
                **self.s3_put_extra_args,
            )

    @staticmethod
    def _build_s3_put_extra_args(s3_cfg: dict[str, Any]) -> dict[str, Any]:
        args: dict[str, Any] = {}
        sse = s3_cfg.get("server_side_encryption")
        if sse:
            args["ServerSideEncryption"] = sse

        kms_key_id = s3_cfg.get("sse_kms_key_id")
        if kms_key_id:
            args["SSEKMSKeyId"] = kms_key_id

        storage_class = s3_cfg.get("storage_class")
        if storage_class:
            args["StorageClass"] = storage_class

        return args


    @staticmethod
    def _extract_folder_name(line: str) -> str:
        line = line.strip()
        m = re.match(r'^\((?P<flags>[^)]*)\)\s+(?P<delim>NIL|"[^"]*")\s+(?P<name>.+)$', line)
        if not m:
            return ""
        name = m.group("name").strip()
        if name.startswith('"') and name.endswith('"') and len(name) >= 2:
            name = name[1:-1].replace(r"\\", "\\").replace(r"\"", '"')
        return name

    @staticmethod
    def _mail_date(msg) -> datetime:
        header = msg.get("Date")
        if not header:
            return datetime.now(tz=ZoneInfo("UTC"))
        try:
            return parsedate_to_datetime(header)
        except Exception:
            return datetime.now(tz=ZoneInfo("UTC"))

    def _strip_attachments(self, msg):
        if msg.is_multipart():
            keep = []
            for part in msg.iter_parts():
                if part.is_attachment():
                    continue
                new_part = self._strip_attachments(part)
                if new_part is not None:
                    keep.append(new_part)
            msg.set_payload(keep)
            return msg
        if msg.is_attachment():
            return None
        return msg

    @staticmethod
    def _to_bytes(msg, account_email: str, folder: str, uid: int) -> bytes:
        buf = BytesIO()
        try:
            BytesGenerator(buf, policy=policy.SMTPUTF8).flatten(msg)
            return buf.getvalue()
        except Exception as exc:
            raise MessageSerializeError(
                f"Failed to serialize message for {account_email} {folder} UID {uid}: {exc}"
            ) from exc

    @staticmethod
    def _read_json(path: Path) -> dict[str, Any]:
        if not path.exists():
            return {}
        return json.loads(path.read_text(encoding="utf-8"))

    @staticmethod
    def _write_json_atomic(path: Path, data: dict[str, Any]) -> None:
        tmp_path = path.with_suffix(f"{path.suffix}.tmp")
        tmp_path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp_path.replace(path)


def setup_logging(cfg: dict[str, Any]) -> None:
    log_cfg = cfg["logging"]
    level = getattr(logging, log_cfg.get("level", "INFO").upper(), logging.INFO)
    log_file = Path(log_cfg["file"])
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(threadName)s %(message)s")

    root = logging.getLogger()
    root.setLevel(level)

    stream = logging.StreamHandler()
    stream.setFormatter(fmt)
    root.addHandler(stream)

    try:
        log_file.parent.mkdir(parents=True, exist_ok=True)
        file_handler = TimedRotatingFileHandler(
            filename=log_file,
            when="midnight",
            backupCount=int(log_cfg.get("retention_days", 30)),
            encoding="utf-8",
        )
        file_handler.setFormatter(fmt)
        root.addHandler(file_handler)
    except OSError as exc:
        print(
            f"Logging file disabled; could not use {log_file}: {exc}",
            file=sys.stderr,
        )


def load_config(path: str) -> dict[str, Any]:
    with open(path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", default="config/config.yaml")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument(
        "--live-progress",
        action="store_true",
        help="Show real-time table with accounts, progress and statuses",
    )
    args = parser.parse_args()

    cfg = load_config(args.config)
    setup_logging(cfg)

    runner = BackupRunner(cfg, dry_run=args.dry_run, live_progress=args.live_progress)
    runner.run()


if __name__ == "__main__":
    main()
