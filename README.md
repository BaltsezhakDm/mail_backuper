# mail_backuper

Инкрементальный бэкап IMAP (Yandex) в локальное хранилище и Selectel S3.

## Возможности

- Ежедневный инкрементальный бэкап по UID.
- Все папки кроме Spam.
- Сохранение писем в `.eml` без вложений.
- Параллельная обработка ящиков (3-5 потоков).
- Ретраи при ошибках (до 3).
- Логи с ротацией и хранением 30 дней.
- Получение секретов аккаунтов из Vault через GitLab JWT.
- Альтернативный способ API-доступа к Vault: через `VAULT_TOKEN` и GET-listing.

## Структура данных

- Локально: `/data/mail_backups/<email>/YYYY/MM/DD/<folder>/<uid>.eml`
- S3: `backups/<email>/YYYY/MM/DD/<folder>/<uid>.eml`

## Закрытый S3 бакет

Для приватного (непубличного) S3-бакета используйте явные параметры клиента в `s3`:

- `access_key_id` / `secret_access_key` / `session_token` — учётные данные (если не берутся из `AWS_*` env).
- `addressing_style: path` — часто нужен для S3-совместимых приватных инсталляций.
- `signature_version: s3v4` — стандартная подпись запросов.
- `verify_ssl` — `true`, `false` или путь до кастомного CA (`/path/to/ca.pem`) для внутреннего TLS.
- `server_side_encryption` / `sse_kms_key_id` — если бакет требует SSE (`AES256` или `aws:kms`).
- `storage_class` — при необходимости принудительного класса хранения.

Пример блока:

```yaml
s3:
  endpoint_url: "https://s3.example.internal"
  region: "ru-1"
  bucket: "private-mail-backups"
  prefix: "backups"
  access_key_id: "${S3_ACCESS_KEY_ID}"
  secret_access_key: "${S3_SECRET_ACCESS_KEY}"
  session_token: null
  addressing_style: "path"
  signature_version: "s3v4"
  verify_ssl: "/etc/ssl/certs/internal-ca.pem"
  server_side_encryption: "AES256"
  sse_kms_key_id: null
  storage_class: null
```

## Настройка

1. Скопируйте `config/config.example.yaml` в `config/config.yaml`.
2. Заполните параметры S3/Vault.
3. Убедитесь, что в GitLab CI доступны `CI_JOB_JWT` или `CI_JOB_JWT_V2`.
4. Примонтируйте volume в контейнер на `/data/mail_backups`.

## Секреты в Vault

Ожидается KV v2 и путь:

- База: `secret/data/mail-backup/accounts`
- Ключи: email-адреса (`user@example.com`)
- Значения:
  - `username`
  - `passwordqzalminveqklfxce`
  - `imap_host` (опционально, по умолчанию `imap.yandex.ru`)
  - `imap_port` (опционально, по умолчанию `993`)

Дополнительные режимы доступа к Vault API:

- `vault.auth_method: jwt` (по умолчанию) — логин через GitLab JWT (`CI_JOB_JWT_V2`/`CI_JOB_JWT`).
- `vault.auth_method: token` — используется токен из переменной окружения `VAULT_TOKEN`.
- `vault.list_method: list` (по умолчанию) — HTTP-метод `LIST` для получения ключей KV v2.
- `vault.list_method: get` — использовать `GET ...?list=true` (полезно, если `LIST` блокируется прокси).
- `vault.list_override_header: true` — при `get` добавляет заголовок `X-HTTP-Method-Override: LIST`.

## Dry-run

```bash
python -m mail_backuper.main --config config/config.yaml --dry-run
```

## Live-progress в реальном времени

Чтобы видеть список почт, текущую папку, прогресс и итоговый статус по каждому ящику в реальном времени, запустите:

```bash
python -m mail_backuper.main --config config/config.yaml --live-progress
```

Можно комбинировать с dry-run:

```bash
python -m mail_backuper.main --config config/config.yaml --dry-run --live-progress
```

## Ежедневный запуск бэкапа

Ниже — два рабочих варианта: `cron` (проще) и `systemd timer` (надежнее и удобнее для мониторинга).

### Вариант 1: cron

1. Убедитесь, что конфиг и секреты доступны в окружении рантайма:
   - `config/config.yaml` заполнен.
   - Для Vault JWT: есть `CI_JOB_JWT` или `CI_JOB_JWT_V2`, либо
   - Для Vault token auth: задан `VAULT_TOKEN`.

2. Создайте скрипт запуска `/usr/local/bin/mail-backuper-run.sh`:

```bash
#!/usr/bin/env bash
set -euo pipefail

cd /opt/mail_backuper
/usr/bin/env python -m mail_backuper.main --config /opt/mail_backuper/config/config.yaml
```

3. Сделайте его исполняемым:

```bash
chmod +x /usr/local/bin/mail-backuper-run.sh
```

4. Добавьте cron-задачу (пример: каждый день в 19:00 по локальному времени сервера):

```bash
crontab -e
```

```cron
0 19 * * * /usr/local/bin/mail-backuper-run.sh >> /var/log/mail-backuper-cron.log 2>&1
```

> Важно: `cron` запускается с минимальным окружением. Если нужны переменные (`VAULT_TOKEN`, AWS/S3 ключи), задайте их в скрипте или через системный механизм секретов.

### Вариант 2: systemd timer (рекомендуется)

1. Создайте unit-файл `/etc/systemd/system/mail-backuper.service`:

```ini
[Unit]
Description=Mail Backuper daily run
After=network-online.target
Wants=network-online.target

[Service]
Type=oneshot
WorkingDirectory=/opt/mail_backuper
ExecStart=/usr/bin/env python -m mail_backuper.main --config /opt/mail_backuper/config/config.yaml
# Пример подгрузки env:
# EnvironmentFile=/etc/mail-backuper.env
```

2. Создайте таймер `/etc/systemd/system/mail-backuper.timer`:

```ini
[Unit]
Description=Run Mail Backuper daily at 19:00

[Timer]
OnCalendar=*-*-* 19:00:00
Persistent=true

[Install]
WantedBy=timers.target
```

3. Активируйте таймер:

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now mail-backuper.timer
```

4. Проверьте статус:

```bash
systemctl status mail-backuper.timer
systemctl list-timers | grep mail-backuper
journalctl -u mail-backuper.service -n 100 --no-pager
```

### Проверка перед продом

- Пробный запуск:

```bash
python -m mail_backuper.main --config config/config.yaml --dry-run --live-progress
```

- Боевой запуск вручную:

```bash
python -m mail_backuper.main --config config/config.yaml --live-progress
```
