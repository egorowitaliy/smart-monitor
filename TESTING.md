# Проверка проекта

## Быстрые проверки

Синтаксис Python:

```bash
python3 -m py_compile smart_monitor.py
```

Разбор примера конфигурации:

```bash
python3 - <<'PY'
import tomllib
with open('config.example.toml', 'rb') as f:
    tomllib.load(f)
print('config.example.toml: OK')
PY
```

Проверка установочного скрипта:

```bash
bash -n install.sh
```

## Регрессионные тесты

Для тестов нужен `pytest`.

Debian/Ubuntu:

```bash
apt install python3-pytest
```

Запуск:

```bash
python3 -m pytest -q
```

Тесты не запускают реальные SMART-самотесты и не требуют физических дисков. Внешние вызовы подменяются тестовыми ответами. В набор входят сценарии зависшего теста и `smartctl -X`, одинаковых записей самотестов ATA в один час, автоматического определения ATA/NVMe, блокировок, порогов температуры/износа, резервного SMS, длинных уведомлений и подтверждения отправки через API.

## Проверка systemd-файлов

После установки программы либо при наличии временной ссылки `/usr/local/sbin/smart-monitor`:

```bash
systemd-analyze verify \
  deploy-examples/systemd/smart-monitor@.service \
  deploy-examples/systemd/smart-monitor-check.timer \
  deploy-examples/systemd/smart-monitor-short.timer \
  deploy-examples/systemd/smart-monitor-long.timer
```

Календарные выражения:

```bash
systemd-analyze calendar '*-*-* 09:00:00'
systemd-analyze calendar 'Sun *-*-* 03:00:00'
systemd-analyze calendar 'Sun *-*-01..07 04:00:00'
```

## Проверка на реальном сервере

Сначала только чтение:

```bash
smart-monitor check none --config /etc/smart-monitor/config.toml
```

Затем короткий тест:

```bash
smart-monitor short none --config /etc/smart-monitor/config.toml
```

После успешного короткого теста проверьте каналы уведомлений по одному и только затем включайте расписание.

Длительный тест лучше запускать в окно низкой нагрузки. На большом HDD он может идти много часов.

## Автоматическая проверка GitHub

Workflow `.github/workflows/tests.yml` запускает синтаксическую проверку, проверку `install.sh` и весь набор `pytest` на Python 3.11, 3.13 и 3.14.
