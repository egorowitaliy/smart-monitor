# systemd и расписание

## Служба

Используется шаблонная служба:

```text
smart-monitor@.service
```

Параметр после `@` становится режимом программы:

```text
smart-monitor@check.service
smart-monitor@short.service
smart-monitor@long.service
```

Проверка вручную:

```bash
systemctl start smart-monitor@check.service
systemctl status smart-monitor@check.service --no-pager --full
```

Для `Type=oneshot` состояние `inactive (dead)` после успешного завершения нормально.

## Коды завершения службы

Код `1` означает, что сама проверка отработала, но обнаружила проблему накопителя или самотеста.

В файле службы указано:

```ini
SuccessExitStatus=1 4
```

Код `4` используется при обычном запуске программы, если общая блокировка занята и время ожидания закончилось. Штатная systemd-служба дополнительно передаёт `--wait-lock`, поэтому она не пропускает пересекающиеся задания, а ждёт освобождения блокировки.

Это особенно важно для таймеров с `Persistent=true`: после долгого выключения сервера несколько пропущенных заданий могут стать готовыми к запуску почти одновременно. Они выстроятся в очередь и выполнятся последовательно. Коды `2` и `3` остаются ошибками службы.

## Таймер ежедневной проверки

```text
smart-monitor-check.timer
```

По умолчанию:

```ini
OnCalendar=*-*-* 09:00:00
```

Проверка читает SMART, но не запускает самотест. Если всё нормально, уведомление не отправляется.

## Таймер короткого теста

```text
smart-monitor-short.timer
```

По умолчанию:

```ini
OnCalendar=Sun *-*-* 03:00:00
```

То есть каждое воскресенье в 03:00.

## Таймер длительного теста

```text
smart-monitor-long.timer
```

По умолчанию:

```ini
OnCalendar=Sun *-*-01..07 04:00:00
```

То есть первое воскресенье каждого месяца в 04:00.

## Включение

```bash
systemctl enable --now \
  smart-monitor-check.timer \
  smart-monitor-short.timer \
  smart-monitor-long.timer
```

## Проверка расписания

```bash
systemctl list-timers 'smart-monitor*' --all --no-pager
```

Проверить отдельное календарное выражение можно так:

```bash
systemd-analyze calendar 'Sun *-*-01..07 04:00:00'
```

## Журнал

Последняя ежедневная проверка:

```bash
journalctl -u smart-monitor@check.service -n 100 --no-pager
```

Короткие тесты:

```bash
journalctl -u smart-monitor@short.service -n 200 --no-pager
```

Длительные тесты:

```bash
journalctl -u smart-monitor@long.service -n 300 --no-pager
```

## Изменение расписания

Скопируйте соответствующий файл таймера или отредактируйте установленный файл через `mcedit`, затем:

```bash
systemctl daemon-reload
systemctl restart smart-monitor-check.timer smart-monitor-short.timer smart-monitor-long.timer
```

Перед применением удобно проверить календарное выражение через `systemd-analyze calendar`.

## Ограничение времени службы

В примере службы используются:

```ini
ExecStart=/usr/local/sbin/smart-monitor %i --wait-lock --config /etc/smart-monitor/config.toml
TimeoutStartSec=infinity
```

Это сделано намеренно. Служба может сначала ждать другой SMART-тест, а затем сама выполнять длительный тест большого HDD, который способен идти дольше восьми часов. Предел для каждого накопителя рассчитывает сама программа; при превышении собственного срока она пытается остановить только запущенный ею тест. Внешний фиксированный таймаут systemd здесь только мешал бы этой логике.
