# mail_backuper

Инкрементальный бэкап IMAP (Yandex) в локальное хранилище и Selectel S3.

## Возможности

- Ежедневный инкрементальный бэкап по UID.
- Все папки кроме Spam.
- Сохранение писем в `.eml` без вложений.
- Параллельная обработка ящиков (3-5 потоков).
- Ретраи при ошибках (до 3).
- Retention локальных бэкапов (по умолчанию 30 дней).
- Логи с ротацией и хранением 30 дней.
- Получение секретов аккаунтов из Vault через GitLab JWT.
- Альтернативный способ API-доступа к Vault: через `VAULT_TOKEN` и GET-listing.

## Структура данных

- Локально: `/data/mail_backups/<email>/YYYY/MM/DD/<folder>/<uid>.eml`
- S3: `backups/<email>/YYYY/MM/DD/<folder>/<uid>.eml`

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
