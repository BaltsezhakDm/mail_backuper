from __future__ import annotations

import argparse
import imaplib
import json
import logging
import re
import sys
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


@dataclass
class Account:
    email: str
    username: str
    password: str
    imap_host: str
    imap_port: int


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
        r = requests.post(url, json={"role": self.role, "jwt": jwt}, timeout=20)
        r.raise_for_status()
        self.token = r.json()["auth"]["client_token"]

    def _headers(self) -> dict[str, str]:
        if not self.token:
            raise RuntimeError("Vault token is missing")
        return {"X-Vault-Token": self.token}

    def list_accounts(self) -> list[str]:
        # KV v2 LIST uses metadata endpoint.
        metadata_path = self.base_path.replace("/data/", "/metadata/", 1)
        url = f"{self.addr}/v1/{metadata_path}?list=true"
        headers = self._headers()
        if self.list_method == "get":
            if self.list_override_header:
                headers["X-HTTP-Method-Override"] = "LIST"
            r = requests.get(url, headers=headers, timeout=20)
        else:
            r = requests.request("LIST", url, headers=headers, timeout=20)
        r.raise_for_status()
        keys = r.json().get("data", {}).get("keys", [])
        return [k.rstrip("/") for k in keys]

    def read_account(self, account_key: str) -> Account:
        url = f"{self.addr}/v1/{self.base_path}/{account_key}"
        r = requests.get(url, headers=self._headers(), timeout=20)
        r.raise_for_status()
        data = r.json()["data"]["data"]
        return Account(
            email=account_key,
            username=data.get("username", account_key),
            password=data["password"],
            imap_host=data.get("imap_host", "imap.yandex.ru"),
            imap_port=int(data.get("imap_port", 993)),
        )


class BackupRunner:
    def __init__(self, cfg: dict[str, Any], dry_run: bool = False):
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

        concurrency = int(self.cfg.get("concurrency", 4))
        continue_on_error = bool(self.cfg.get("limits", {}).get("continue_on_account_error", True))

        with ThreadPoolExecutor(max_workers=concurrency) as pool:
            futures = {
                pool.submit(self.backup_one_account, vault.read_account(key)): key for key in keys
            }
            for f in as_completed(futures):
                key = futures[f]
                try:
                    f.result()
                except Exception as exc:
                    logging.exception("Account %s failed: %s", key, exc)
                    if not continue_on_error:
                        raise

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
            for folder_raw in folders:
                folder_name = self._extract_folder_name(folder_raw.decode("utf-8", errors="ignore"))
                if not folder_name or folder_name.lower() in self.skip_folders:
                    continue
                self._backup_folder(account, imap, folder_name, state)

        if not self.dry_run:
            state_path.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
        logging.info("Done backup for %s", account.email)

    def _backup_folder(
        self,
        account: Account,
        imap: imaplib.IMAP4_SSL,
        folder: str,
        state: dict[str, Any],
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
        if not uid_list:
            return

        max_uid = last_uid
        for uid_b in uid_list:
            uid = int(uid_b.decode())
            self._backup_uid(account, imap, folder, uid)
            max_uid = max(max_uid, uid)

        folder_state["last_uid"] = max_uid

    def _backup_uid(self, account: Account, imap: imaplib.IMAP4_SSL, folder: str, uid: int) -> None:
        status, data = imap.uid("FETCH", str(uid), "(RFC822 INTERNALDATE)")
        if status != "OK" or not data or data[0] is None:
            return

        raw = data[0][1]
        msg = BytesParser(policy=policy.default).parsebytes(raw)
        stripped = self._strip_attachments(msg)
        if stripped is None:
            return

        day = self._mail_date(stripped).astimezone(self.tz)
        day_path = day.strftime("%Y/%m/%d")
        folder_sane = folder.replace("/", "_")

        local_dir = self.local_root / account.email / day_path / folder_sane
        local_dir.mkdir(parents=True, exist_ok=True)
        local_file = local_dir / f"{uid}.eml"

        eml_bytes = self._to_bytes(stripped)

        if not self.dry_run:
            local_file.write_bytes(eml_bytes)

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
    def _to_bytes(msg) -> bytes:
        buf = BytesIO()
        BytesGenerator(buf, policy=policy.SMTP).flatten(msg)
        return buf.getvalue()

    @staticmethod
    def _read_json(path: Path) -> dict[str, Any]:
        if not path.exists():
            return {}
        return json.loads(path.read_text(encoding="utf-8"))


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
    args = parser.parse_args()

    cfg = load_config(args.config)
    setup_logging(cfg)

    runner = BackupRunner(cfg, dry_run=args.dry_run)
    runner.run()


if __name__ == "__main__":
    main()
