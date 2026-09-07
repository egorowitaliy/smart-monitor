#!/usr/bin/env bash

fail() {
    printf 'Ошибка: %s\n' "$1" >&2
    return 1
}

main() {
    if [ "$(id -u)" -ne 0 ]; then
        fail 'установку нужно запускать от root.'
        return 1
    fi

    SRC_DIR="$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)" || {
        fail 'не удалось определить каталог исходников.'
        return 1
    }
    APP_DIR="/opt/smart-monitor"
    CONFIG_DIR="/etc/smart-monitor"
    SYSTEMD_DIR="/etc/systemd/system"

    if ! command -v python3 >/dev/null 2>&1; then
        fail 'python3 не найден.'
        return 1
    fi

    if ! command -v smartctl >/dev/null 2>&1 && [ ! -x /usr/sbin/smartctl ]; then
        fail 'smartctl не найден. Установите пакет smartmontools.'
        return 1
    fi

    if ! command -v systemctl >/dev/null 2>&1; then
        fail 'systemctl не найден. Для install.sh требуется systemd.'
        return 1
    fi

    if ! python3 - <<'PY'
import sys
if sys.version_info < (3, 11):
    raise SystemExit('Ошибка: требуется Python 3.11 или новее.')
PY
    then
        return 1
    fi

    if ! python3 -m py_compile "$SRC_DIR/smart_monitor.py"; then
        fail 'smart_monitor.py не прошёл проверку синтаксиса.'
        return 1
    fi

    if ! python3 - "$SRC_DIR/config.example.toml" <<'PY'
import sys
import tomllib
with open(sys.argv[1], 'rb') as f:
    tomllib.load(f)
PY
    then
        fail 'config.example.toml содержит ошибку.'
        return 1
    fi

    if ! install -d -m 0755 "$APP_DIR"; then
        fail "не удалось создать $APP_DIR."
        return 1
    fi

    if ! install -m 0755 "$SRC_DIR/smart_monitor.py" "$APP_DIR/smart_monitor.py"; then
        fail 'не удалось установить smart_monitor.py.'
        return 1
    fi

    if [ -e /usr/local/sbin/smart-monitor ] && [ ! -L /usr/local/sbin/smart-monitor ]; then
        fail '/usr/local/sbin/smart-monitor уже существует и не является символической ссылкой.'
        return 1
    fi

    if ! ln -sfn "$APP_DIR/smart_monitor.py" /usr/local/sbin/smart-monitor; then
        fail 'не удалось создать /usr/local/sbin/smart-monitor.'
        return 1
    fi

    if ! install -d -m 0700 "$CONFIG_DIR"; then
        fail "не удалось создать $CONFIG_DIR."
        return 1
    fi

    if [ ! -e "$CONFIG_DIR/config.toml" ]; then
        if ! install -m 0600 "$SRC_DIR/config.example.toml" "$CONFIG_DIR/config.toml"; then
            fail 'не удалось создать config.toml.'
            return 1
        fi
        printf '%s\n' "Создан пример конфигурации: $CONFIG_DIR/config.toml"
    else
        printf '%s\n' "Конфигурация уже существует и не изменена: $CONFIG_DIR/config.toml"
    fi

    if [ ! -e "$CONFIG_DIR/secrets.env" ]; then
        if ! install -m 0600 "$SRC_DIR/deploy-examples/secrets.env.example" "$CONFIG_DIR/secrets.env"; then
            fail 'не удалось создать secrets.env.'
            return 1
        fi
    fi

    for name in \
        smart-monitor@.service \
        smart-monitor-check.timer \
        smart-monitor-short.timer \
        smart-monitor-long.timer
    do
        if ! install -m 0644 \
            "$SRC_DIR/deploy-examples/systemd/$name" \
            "$SYSTEMD_DIR/$name"
        then
            fail "не удалось установить $name."
            return 1
        fi
    done

    if ! systemctl daemon-reload; then
        fail 'systemctl daemon-reload завершился с ошибкой.'
        return 1
    fi

    printf '\n%s\n' 'Установка завершена.'
    printf '%s\n' '1. Отредактируйте /etc/smart-monitor/config.toml'
    printf '%s\n' '2. Выполните: smart-monitor check none --config /etc/smart-monitor/config.toml'
    printf '%s\n' '3. Настройте default_destination, если нужны автоматические уведомления'
    printf '%s\n' '4. После проверки включите таймеры согласно SYSTEMD.md'
}

main "$@"
