# Конфигурация

Основной файл:

```text
/etc/smart-monitor/config.toml
```

В нём могут находиться токены доступа, поэтому рекомендуемые права:

```bash
chmod 600 /etc/smart-monitor/config.toml
```

## Общие параметры

```toml
[general]
hostname = ""
default_destination = "none"
smartctl_path = ""
lock_file = "/run/smart-monitor.lock"
lock_wait_seconds = 5
smartctl_command_timeout_seconds = 30
poll_interval_seconds = 20
poll_error_limit = 3
selftest_result_settle_seconds = 30
abort_verify_seconds = 30
ata_short_grace_minutes = 10
ata_long_grace_minutes = 60
nvme_short_timeout_minutes = 30
nvme_long_timeout_minutes = 360
```

`hostname` — имя сервера в отчёте. Если оставить пустым, берётся имя системы.

`default_destination` — набор каналов для автоматических уведомлений:

- `telegram`;
- `matrix`;
- `max`;
- `telegram-matrix`;
- `telegram-max`;
- `matrix-max`;
- `all`;
- `none`.

`smartctl_path` — путь к `smartctl`. Обычно оставляется пустым: программа ищет команду сама.

`lock_file` — блокировка самого SMART Monitor. Она не позволяет двум экземплярам одновременно запускать или контролировать тесты.

`lock_wait_seconds` — сколько ждать освобождения этой блокировки при обычном ручном запуске. Штатная systemd-служба использует `--wait-lock` и ждёт без ограничения времени, чтобы несколько пропущенных `Persistent`-заданий выполнились последовательно.

`smartctl_command_timeout_seconds` — максимальное время одного вызова `smartctl`.

`poll_interval_seconds` — интервал проверки хода самотеста.

`poll_error_limit` — сколько подряд ошибок опроса допускается до аварийной остановки запущенного программой теста.

`selftest_result_settle_seconds` — сколько ждать появления новой записи в журнале самотестов после того, как накопитель уже сообщил о завершении теста.

`abort_verify_seconds` — сколько ждать подтверждения остановки после `smartctl -X`.

`ata_short_grace_minutes` и `ata_long_grace_minutes` — запас к расчётному времени ATA-теста, которое сообщает сам накопитель.

`nvme_short_timeout_minutes` и `nvme_long_timeout_minutes` — предел ожидания NVMe-тестов, когда подходящего расчётного времени нет.

## Накопители

Для каждого накопителя создаётся отдельный блок `[[disks]]`.

Пример SATA HDD:

```toml
[[disks]]
path = "/dev/disk/by-id/ata-WDC_EXAMPLE_DISK_1"
label = "Диск данных"
kind = "auto"
temperature_warning = 50
temperature_critical = 60
```

Пример NVMe:

```toml
[[disks]]
path = "/dev/disk/by-id/nvme-EXAMPLE_NVME_1"
label = "Системный SSD"
kind = "auto"
temperature_warning = 70
temperature_critical = 80
wear_warning_percent = 90
wear_critical_percent = 100
```

Рекомендуется использовать `/dev/disk/by-id/...`, а не `/dev/sda`, `/dev/sdb`: буквенное имя диска может измениться после перезагрузки или изменения состава оборудования.


### Тип накопителя

`kind` может принимать значения:

- `auto` — определить ATA или NVMe через `smartctl -i`; это рекомендуемый вариант;
- `ata` — принудительно считать накопитель ATA/SATA;
- `nvme` — принудительно считать накопитель NVMe.

Если `kind` не указан, используется `auto`. Для необычных USB-мостов или оборудования с нестандартным ответом `smartctl` тип можно указать вручную.

### USB/SATA-мосты

Некоторые внешние корпуса и переходники требуют явного типа устройства для `smartctl`.

Например:

```toml
[[disks]]
path = "/dev/disk/by-id/usb-WD_EXAMPLE_DISK-0:0"
label = "Внешний HDD"
kind = "auto"
device_type = "sat"
temperature_warning = 50
temperature_critical = 60
```

Это соответствует параметру:

```bash
smartctl -d sat ...
```

Нужное значение можно определить обычным `smartctl --scan-open` и ручной проверкой конкретного устройства.


### Пороги температуры и износа

Пороги задаются отдельно для каждого накопителя. Это намеренно: нормальная температура HDD и NVMe заметно отличается, а допустимые значения зависят от конкретной модели и условий охлаждения.

Для ATA/SATA можно задать:

```toml
temperature_warning = 50
temperature_critical = 60
```

Для NVMe дополнительно доступны:

```toml
wear_warning_percent = 90
wear_critical_percent = 100
```

Если порог не указан, соответствующая проверка отключена. Значения в примере — стартовые, а не универсальная норма для любого накопителя. Для окончательной настройки ориентируйтесь на документацию производителя конкретного диска.

## Telegram

```toml
[telegram]
enabled = true
chat_id = "123456789"
token = ""
token_env = "SMART_MONITOR_TELEGRAM_TOKEN"
proxy_enabled = false
proxy_url = "http://127.0.0.1:3128"
proxy_username = ""
proxy_password = ""
proxy_password_env = "SMART_MONITOR_TELEGRAM_PROXY_PASSWORD"
```

Токен можно хранить непосредственно в `token` либо вынести в переменную окружения через `token_env`.

Если указан `token_env`, он имеет приоритет над `token`.

## Matrix

```toml
[matrix]
enabled = true
url = "https://matrix.example.org"
room_id = "!roomid:example.org"
token = ""
token_env = "SMART_MONITOR_MATRIX_TOKEN"
```

Для локального Synapse допустим внутренний HTTP-адрес, например адрес Docker-сети. Для подключения через внешнюю сеть используйте HTTPS.

## MAX

```toml
[max]
enabled = true
api_host = "platform-api2.max.ru"
api_port = 443
chat_id = ""
bot_token = ""
bot_token_env = "SMART_MONITOR_MAX_TOKEN"
```

## Резервное SMS через Huawei

```toml
[sms_fallback]
enabled = true
modem_url = "http://192.168.8.1"
phone = "+70000000000"
lock_file = "/run/lock/huawei-modem-api.lock"
lock_wait_seconds = 30
cleanup_sent_message = true
```

Этот режим рассчитан на устройства с совместимым Huawei HiLink API.

`lock_file` должен указывать на один и тот же файл блокировки для всех программ, которые работают с этим модемом. Если модем используется из нескольких контейнеров, подключите в них один общий каталог хоста и храните файл блокировки в нём.

При `cleanup_sent_message = true` программа удаляет только то новое сообщение, которое однозначно соответствует только что отправленному ею SMS. Если однозначно определить сообщение нельзя, удаление пропускается.

## Секреты через systemd

Пример `/etc/smart-monitor/secrets.env`:

```text
SMART_MONITOR_TELEGRAM_TOKEN=...
SMART_MONITOR_TELEGRAM_PROXY_PASSWORD=...
SMART_MONITOR_MATRIX_TOKEN=...
SMART_MONITOR_MAX_TOKEN=...
```

Права:

```bash
chmod 600 /etc/smart-monitor/secrets.env
```
