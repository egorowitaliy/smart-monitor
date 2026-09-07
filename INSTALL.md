# Установка

## Рекомендуемая схема

```text
/opt/smart-monitor/smart_monitor.py
/etc/smart-monitor/config.toml
/etc/smart-monitor/secrets.env
/usr/local/sbin/smart-monitor -> /opt/smart-monitor/smart_monitor.py
/etc/systemd/system/smart-monitor@.service
/etc/systemd/system/smart-monitor-*.timer
```

Исходный репозиторий можно хранить в любом удобном каталоге. Рабочая копия Git не должна смешиваться с установленной программой и конфигурацией.

## Debian и Ubuntu

```bash
apt update
apt install python3 smartmontools
```

Требуется Python 3.11 или новее. Для самотестов NVMe нужен smartmontools 7.4 или новее; для ATA проверяйте совместимость установленной версии `smartctl` с вашим оборудованием. Скрипт `install.sh` устанавливает службу и таймеры systemd, поэтому рассчитан на системы с systemd.

Проверьте версии:

```bash
python3 --version
smartctl --version
```

## Установка из репозитория

```bash
git clone https://github.com/egorowitaliy/smart-monitor.git
cd smart-monitor
./install.sh
```

Скрипт установки:

- копирует программу в `/opt/smart-monitor`;
- создаёт ссылку `/usr/local/sbin/smart-monitor`;
- создаёт `/etc/smart-monitor/config.toml`, только если файла ещё нет;
- создаёт `/etc/smart-monitor/secrets.env`, только если файла ещё нет;
- устанавливает службу и файлы таймеров systemd;
- не включает расписание автоматически.

После установки откройте конфигурацию:

```bash
mcedit /etc/smart-monitor/config.toml
```

Если секреты вынесены в отдельный файл:

```bash
mcedit /etc/smart-monitor/secrets.env
chmod 600 /etc/smart-monitor/config.toml /etc/smart-monitor/secrets.env
```

## Первая проверка

Сначала только чтение SMART:

```bash
smart-monitor check none --config /etc/smart-monitor/config.toml
```

Ожидаемый код при исправных накопителях:

```bash
echo $?
```

```text
0
```

Затем короткий самотест:

```bash
smart-monitor short none --config /etc/smart-monitor/config.toml
```

После этого можно проверить уведомления:

```bash
smart-monitor check all --notify --config /etc/smart-monitor/config.toml
```

## Включение расписания

```bash
systemctl enable --now \
  smart-monitor-check.timer \
  smart-monitor-short.timer \
  smart-monitor-long.timer
```

Проверка:

```bash
systemctl list-timers 'smart-monitor*' --all --no-pager
systemctl --failed --no-pager
```

## Обновление

Получите новую версию в рабочем репозитории и повторно запустите установку:

```bash
cd smart-monitor
git pull
./install.sh
```

Существующие `/etc/smart-monitor/config.toml` и `/etc/smart-monitor/secrets.env` не перезаписываются.

После обновления:

```bash
python3 -m py_compile /opt/smart-monitor/smart_monitor.py
systemd-analyze verify /etc/systemd/system/smart-monitor@.service
smart-monitor check none --config /etc/smart-monitor/config.toml
```

## Удаление

Остановите и отключите таймеры:

```bash
systemctl disable --now \
  smart-monitor-check.timer \
  smart-monitor-short.timer \
  smart-monitor-long.timer
```

Удалите программу и файлы systemd:

```bash
rm -f /usr/local/sbin/smart-monitor
rm -rf /opt/smart-monitor
rm -f \
  /etc/systemd/system/smart-monitor@.service \
  /etc/systemd/system/smart-monitor-check.timer \
  /etc/systemd/system/smart-monitor-short.timer \
  /etc/systemd/system/smart-monitor-long.timer
systemctl daemon-reload
```

Конфигурацию `/etc/smart-monitor` удаляйте отдельно только если она больше не нужна.
