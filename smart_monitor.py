#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Vitaliy Egorov

from __future__ import annotations

import argparse
import fcntl
import json
import logging
import os
import signal
import shutil
import socket
import subprocess
import sys
import time
import tomllib
import urllib.error
import urllib.parse
import urllib.request
import uuid
import xml.etree.ElementTree as ET

from dataclasses import dataclass, field, replace
from enum import Enum
from pathlib import Path
from typing import Any, BinaryIO


DEFAULT_SMARTCTL = "/usr/sbin/smartctl"
VERSION = "1.0.1"
DEFAULT_CONFIG = "/etc/smart-monitor/config.toml"
LOG = logging.getLogger("smart-monitor")

EXIT_OK = 0
EXIT_FINDING = 1
EXIT_RUNTIME_ERROR = 2
EXIT_NOTIFICATION_ERROR = 3
EXIT_BUSY = 4

SMARTCTL_COMMAND_ERROR_MASK = 0x07
SMARTCTL_DISK_FAILING = 1 << 3
SMARTCTL_PREFAIL_NOW = 1 << 4
SMARTCTL_PREFAIL_PAST = 1 << 5
SMARTCTL_ERROR_LOG = 1 << 6
SMARTCTL_SELFTEST_LOG = 1 << 7

VALID_DESTINATIONS = {
    "telegram",
    "matrix",
    "max",
    "telegram-matrix",
    "telegram-max",
    "matrix-max",
    "all",
    "none",
}

_STOP_REQUESTED = False


class MonitorError(Exception):
    pass


class SelfTestOutcome(str, Enum):
    NOT_RUN = "not-run"
    PASSED = "passed"
    FAILED = "failed"
    TIMEOUT_ABORTED = "timeout-aborted"
    TIMEOUT_ABORT_FAILED = "timeout-abort-failed"
    BUSY = "busy"
    START_FAILED = "start-failed"
    MONITOR_FAILED = "monitor-failed"
    INTERRUPTED = "interrupted"
    RESULT_MISSING = "result-missing"


@dataclass(frozen=True)
class Disk:
    path: str
    label: str
    kind: str
    device_type: str | None = None
    temperature_warning: int | None = None
    temperature_critical: int | None = None
    wear_warning_percent: int | None = None
    wear_critical_percent: int | None = None


@dataclass
class DiskStatus:
    disk: Disk
    present: bool = False
    severity: int = 2
    status_text: str = "Плохо"
    temperature: int | None = None
    power_on_hours: int | None = None
    metrics: dict[str, int | None] = field(default_factory=dict)
    issues: list[str] = field(default_factory=list)
    smartctl_exit_status: int = 0
    operational_error: bool = False


@dataclass
class SelfTestResult:
    disk: Disk
    test_type: str
    outcome: SelfTestOutcome = SelfTestOutcome.NOT_RUN
    started: bool = False
    owned: bool = False
    completed: bool = False
    detail: str = ""
    timeout_minutes: int = 0
    recommended_minutes: int | None = None
    remaining_percent: int | None = None
    abort_attempted: bool = False
    abort_succeeded: bool = False

    @property
    def passed(self) -> bool:
        return self.outcome == SelfTestOutcome.PASSED

    @property
    def finding(self) -> bool:
        return self.outcome in {
            SelfTestOutcome.FAILED,
            SelfTestOutcome.TIMEOUT_ABORTED,
            SelfTestOutcome.BUSY,
        }

    @property
    def operational_failure(self) -> bool:
        return self.outcome in {
            SelfTestOutcome.TIMEOUT_ABORT_FAILED,
            SelfTestOutcome.START_FAILED,
            SelfTestOutcome.MONITOR_FAILED,
            SelfTestOutcome.INTERRUPTED,
            SelfTestOutcome.RESULT_MISSING,
        }


@dataclass
class SelfTestState:
    in_progress: bool
    description: str = ""
    remaining_percent: int | None = None
    data: dict[str, Any] = field(default_factory=dict)


@dataclass
class ActiveTest:
    result: SelfTestResult
    before_signature: str
    deadline: float
    poll_errors: int = 0
    last_error: str = ""
    inactive_since: float | None = None


@dataclass
class ChannelAttempt:
    name: str
    enabled: bool
    success: bool | None
    detail: str = ""


# ---------------------------------------------------------------------------
# Конфигурация и общие функции
# ---------------------------------------------------------------------------


def _get_int(mapping: dict[str, Any], key: str, default: int, minimum: int = 1) -> int:
    value = mapping.get(key, default)
    if not isinstance(value, int) or isinstance(value, bool) or value < minimum:
        return default
    return value


def _get_bool(mapping: dict[str, Any], key: str, default: bool) -> bool:
    value = mapping.get(key, default)
    return value if isinstance(value, bool) else default


def get_int(value: Any) -> int | None:
    if isinstance(value, int) and not isinstance(value, bool):
        return value
    return None


def optional_int(
    mapping: dict[str, Any],
    key: str,
    *,
    minimum: int,
    maximum: int,
) -> int | None:
    value = mapping.get(key)
    if value is None:
        return None
    if (
        not isinstance(value, int)
        or isinstance(value, bool)
        or not minimum <= value <= maximum
    ):
        raise MonitorError(
            f"Параметр {key} должен быть целым числом "
            f"от {minimum} до {maximum}"
        )
    return value


def load_config(path: Path) -> dict[str, Any]:
    try:
        with path.open("rb") as fh:
            config = tomllib.load(fh)
    except FileNotFoundError as exc:
        raise MonitorError(f"Не найден конфиг: {path}") from exc
    except tomllib.TOMLDecodeError as exc:
        raise MonitorError(f"Ошибка TOML в {path}: {exc}") from exc
    except OSError as exc:
        raise MonitorError(f"Не удалось прочитать конфиг {path}: {exc}") from exc

    try:
        mode = path.stat().st_mode & 0o777
        if mode & 0o077:
            LOG.warning(
                "Конфиг %s имеет права %03o; в нём могут находиться секреты",
                path,
                mode,
            )
    except OSError:
        pass

    return config


def load_disks(config: dict[str, Any]) -> list[Disk]:
    raw_disks = config.get("disks", [])
    if not isinstance(raw_disks, list):
        raise MonitorError("Параметр disks должен быть массивом TOML")

    disks: list[Disk] = []
    seen_paths: set[str] = set()
    seen_labels: set[str] = set()

    for item in raw_disks:
        if not isinstance(item, dict):
            raise MonitorError("Каждый элемент disks должен быть таблицей TOML")

        path = str(item.get("path", "")).strip()
        label = str(item.get("label", "")).strip()
        kind = str(item.get("kind", "auto")).strip().lower()
        device_type_raw = item.get("device_type")
        device_type = None if device_type_raw is None else str(device_type_raw).strip()
        temperature_warning = optional_int(
            item,
            "temperature_warning",
            minimum=1,
            maximum=120,
        )
        temperature_critical = optional_int(
            item,
            "temperature_critical",
            minimum=1,
            maximum=120,
        )
        wear_warning_percent = optional_int(
            item,
            "wear_warning_percent",
            minimum=1,
            maximum=100,
        )
        wear_critical_percent = optional_int(
            item,
            "wear_critical_percent",
            minimum=1,
            maximum=100,
        )

        if not path:
            raise MonitorError("У диска не указан path")
        if not label:
            raise MonitorError(f"У диска {path} не указан label")
        if kind not in {"ata", "nvme", "auto"}:
            raise MonitorError(f"{label}: неизвестный kind={kind!r}")
        if device_type is not None:
            if (
                not device_type
                or any(ch.isspace() for ch in device_type)
                or len(device_type) > 64
            ):
                raise MonitorError(
                    f"{label}: некорректный device_type={device_type!r}"
                )
        if (
            temperature_warning is not None
            and temperature_critical is not None
            and temperature_critical <= temperature_warning
        ):
            raise MonitorError(
                f"{label}: temperature_critical должен быть выше "
                "temperature_warning"
            )
        if (
            wear_warning_percent is not None
            and wear_critical_percent is not None
            and wear_critical_percent <= wear_warning_percent
        ):
            raise MonitorError(
                f"{label}: wear_critical_percent должен быть выше "
                "wear_warning_percent"
            )
        if kind == "ata" and (
            wear_warning_percent is not None
            or wear_critical_percent is not None
        ):
            raise MonitorError(
                f"{label}: пороги износа применимы только к NVMe"
            )
        if path in seen_paths:
            raise MonitorError(f"Диск {path} указан в конфиге несколько раз")
        if label in seen_labels:
            raise MonitorError(f"Метка диска {label!r} указана несколько раз")

        seen_paths.add(path)
        seen_labels.add(label)
        disks.append(
            Disk(
                path=path,
                label=label,
                kind=kind,
                device_type=device_type,
                temperature_warning=temperature_warning,
                temperature_critical=temperature_critical,
                wear_warning_percent=wear_warning_percent,
                wear_critical_percent=wear_critical_percent,
            )
        )

    if not disks:
        raise MonitorError("В config.toml не настроено ни одного диска")

    return disks


def validate_general(config: dict[str, Any]) -> dict[str, Any]:
    general = config.get("general", {})
    if not isinstance(general, dict):
        raise MonitorError("Секция [general] должна быть TOML-таблицей")

    destination = str(general.get("default_destination", "none")).strip()
    if destination not in VALID_DESTINATIONS:
        raise MonitorError(f"Неизвестный default_destination={destination!r}")

    return general


def acquire_lock(
    path: str,
    *,
    wait_seconds: float | None = 0.0,
) -> BinaryIO | None:
    lock_path = Path(path)

    try:
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        handle = lock_path.open("a+b")
    except OSError as exc:
        raise MonitorError(f"Не удалось открыть lock-файл {path}: {exc}") from exc

    deadline = (
        None
        if wait_seconds is None
        else time.monotonic() + max(0.0, wait_seconds)
    )

    while True:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            return handle
        except BlockingIOError:
            if _STOP_REQUESTED:
                handle.close()
                return None
            if deadline is not None and time.monotonic() >= deadline:
                handle.close()
                return None
            time.sleep(0.2)
        except OSError as exc:
            handle.close()
            raise MonitorError(f"Не удалось установить lock {path}: {exc}") from exc


def resolve_disk_device(disk: Disk) -> str:
    path = Path(disk.path)
    if not path.exists():
        raise MonitorError(f"{disk.label}: устройство не найдено: {disk.path}")

    try:
        return str(path.resolve(strict=True))
    except FileNotFoundError as exc:
        raise MonitorError(f"{disk.label}: устройство не найдено: {disk.path}") from exc


def _signal_handler(signum: int, _frame: Any) -> None:
    global _STOP_REQUESTED
    _STOP_REQUESTED = True
    LOG.warning("Получен сигнал %s; завершаем мониторинг безопасно", signum)


def install_signal_handlers() -> None:
    signal.signal(signal.SIGTERM, _signal_handler)
    signal.signal(signal.SIGINT, _signal_handler)


# ---------------------------------------------------------------------------
# Вызовы smartctl и оценка состояния SMART
# ---------------------------------------------------------------------------




def smartctl_binary(general: dict[str, Any]) -> str:
    configured = str(general.get("smartctl_path", "")).strip()
    if configured:
        return configured
    return shutil.which("smartctl") or DEFAULT_SMARTCTL


def smartctl_device_args(disk: Disk) -> list[str]:
    if disk.device_type:
        return ["-d", disk.device_type]
    return []


def secret_value(section: dict[str, Any], key: str) -> str:
    env_name = str(section.get(f"{key}_env", "")).strip()
    if env_name:
        value = os.environ.get(env_name)
        if value is not None and value.strip():
            return value.strip()
    return str(section.get(key, "")).strip()


def smartctl_command_timeout(general: dict[str, Any]) -> int:
    return _get_int(general, "smartctl_command_timeout_seconds", 30, 5)


def run_smartctl_json(
    disk: Disk,
    general: dict[str, Any],
    *arguments: str,
) -> tuple[dict[str, Any], int]:
    device = resolve_disk_device(disk)
    command = [smartctl_binary(general), *smartctl_device_args(disk), "-j", *arguments, device]
    LOG.debug("CMD: %s", " ".join(command))

    try:
        result = subprocess.run(
            command,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
            timeout=smartctl_command_timeout(general),
        )
    except subprocess.TimeoutExpired as exc:
        raise MonitorError(
            f"{disk.label}: smartctl не завершился за "
            f"{smartctl_command_timeout(general)} секунд"
        ) from exc
    except OSError as exc:
        raise MonitorError(f"{disk.label}: не удалось запустить smartctl: {exc}") from exc

    try:
        data = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        detail = result.stderr.strip() or f"exit code {result.returncode}"
        raise MonitorError(
            f"{disk.label}: smartctl не вернул корректный JSON: {detail}"
        ) from exc

    exit_status = data.get("smartctl", {}).get("exit_status", result.returncode)
    if not isinstance(exit_status, int) or isinstance(exit_status, bool):
        exit_status = result.returncode

    return data, exit_status


def run_smartctl_raw(
    disk: Disk,
    general: dict[str, Any],
    *arguments: str,
) -> subprocess.CompletedProcess[str]:
    device = resolve_disk_device(disk)
    command = [smartctl_binary(general), *smartctl_device_args(disk), *arguments, device]
    LOG.debug("CMD: %s", " ".join(command))

    try:
        return subprocess.run(
            command,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
            timeout=smartctl_command_timeout(general),
        )
    except subprocess.TimeoutExpired as exc:
        raise MonitorError(
            f"{disk.label}: smartctl не завершился за "
            f"{smartctl_command_timeout(general)} секунд"
        ) from exc
    except OSError as exc:
        raise MonitorError(f"{disk.label}: ошибка запуска smartctl: {exc}") from exc


def infer_disk_kind(data: dict[str, Any]) -> str | None:
    device = data.get("device", {})
    if not isinstance(device, dict):
        return None

    protocol = str(device.get("protocol", "")).strip().lower()
    device_type = str(device.get("type", "")).strip().lower()
    combined = f"{protocol} {device_type}"

    if "nvme" in combined:
        return "nvme"
    if "ata" in combined or "sat" in combined:
        return "ata"
    return None


def validate_detected_kind(disk: Disk, detected: str) -> None:
    if detected == "ata" and (
        disk.wear_warning_percent is not None
        or disk.wear_critical_percent is not None
    ):
        raise MonitorError(
            f"{disk.label}: пороги износа применимы только к NVMe, "
            "но автоматически определён ATA-накопитель"
        )


def detect_disk_kind(disk: Disk, general: dict[str, Any]) -> Disk:
    if disk.kind != "auto":
        return disk

    try:
        data, exit_status = run_smartctl_json(disk, general, "-i")
    except MonitorError as exc:
        # Не обрываем проверку всех дисков из-за одного пропавшего или
        # недоступного устройства. read_disk_status() ниже сформирует
        # полноценную ошибку и позволит отправить уведомление.
        LOG.warning("%s: тип накопителя пока не определён: %s", disk.label, exc)
        return disk

    command_error_bits = exit_status & SMARTCTL_COMMAND_ERROR_MASK
    if command_error_bits:
        messages = smartctl_messages(data)
        LOG.warning(
            "%s: тип накопителя пока не определён (smartctl bits=0x%02x)%s",
            disk.label,
            command_error_bits,
            f": {messages}" if messages else "",
        )
        return disk

    detected = infer_disk_kind(data)
    if detected is None:
        LOG.warning(
            "%s: smartctl не позволил однозначно определить тип накопителя",
            disk.label,
        )
        return disk

    validate_detected_kind(disk, detected)
    LOG.info("%s: тип накопителя определён автоматически: %s", disk.label, detected)
    return replace(disk, kind=detected)


def detect_disk_kinds(
    disks: list[Disk],
    general: dict[str, Any],
) -> list[Disk]:
    return [detect_disk_kind(disk, general) for disk in disks]

def smartctl_messages(data: dict[str, Any]) -> str:
    messages: list[str] = []
    items = data.get("smartctl", {}).get("messages", [])
    if not isinstance(items, list):
        return ""

    for item in items:
        if not isinstance(item, dict):
            continue
        text = str(item.get("string", "")).strip()
        if text:
            messages.append(text)

    return "; ".join(messages)


def get_ata_raw_value(data: dict[str, Any], attribute_id: int) -> int | None:
    attributes = data.get("ata_smart_attributes", {}).get("table", [])
    if not isinstance(attributes, list):
        return None

    for attribute in attributes:
        if not isinstance(attribute, dict) or attribute.get("id") != attribute_id:
            continue
        value = attribute.get("raw", {}).get("value")
        return get_int(value)

    return None


def get_temperature(data: dict[str, Any]) -> int | None:
    return get_int(data.get("temperature", {}).get("current"))


def get_power_on_hours(data: dict[str, Any]) -> int | None:
    return get_int(data.get("power_on_time", {}).get("hours"))


def _add_issue(target: list[str], text: str) -> None:
    if text not in target:
        target.append(text)


def smartctl_health_bits(exit_status: int) -> tuple[list[str], list[str]]:
    critical: list[str] = []
    warning: list[str] = []

    if exit_status & SMARTCTL_DISK_FAILING:
        critical.append("SMART status сообщает о предстоящем отказе")
    if exit_status & SMARTCTL_PREFAIL_NOW:
        critical.append("SMART-атрибут категории Pre-fail достиг порога")
    if exit_status & SMARTCTL_PREFAIL_PAST:
        warning.append("SMART-атрибут ранее достигал порога")
    if exit_status & SMARTCTL_ERROR_LOG:
        warning.append("Журнал ошибок SMART содержит записи")

    # Бит SMARTCTL_SELFTEST_LOG намеренно не превращаем в постоянное
    # предупреждение: он отражает наличие ошибок во всей истории журнала.
    # Текущее состояние самотестов оценивается по последней записи ниже.
    return critical, warning


def apply_latest_selftest_health(
    status: DiskStatus,
    data: dict[str, Any],
    critical: list[str],
    warning: list[str],
) -> None:
    table = selftest_table(status.disk, data)
    if not table:
        return

    latest = table[0]

    if status.disk.kind == "ata":
        result = latest.get("status", {})
        if not isinstance(result, dict):
            return
        passed = result.get("passed")
        value = get_int(result.get("value"))
        description = str(result.get("string", "")).strip()
        lowered = description.lower()

        # В SMART self-test log поле "passed" присутствует не всегда.
        # Например, smartctl 7.4 для "Aborted by host" возвращает только
        # value/string/remaining_percent. Поэтому учитываем ATA status code:
        # high nibble 0 = успешно, 1 = aborted by host, 2 = interrupted/reset.
        status_code = ((value >> 4) & 0x0F) if value is not None else None

        if (
            passed is True
            or status_code == 0
            or "completed without error" in lowered
        ):
            return

        if (
            status_code in (1, 2)
            or "aborted by host" in lowered
            or "interrupted" in lowered
            or "host reset" in lowered
        ):
            _add_issue(
                warning,
                "Последний самотест ATA был прерван"
                + (f": {description}" if description else ""),
            )
        elif passed is False or status_code is not None:
            _add_issue(
                critical,
                "Последний самотест ATA завершился с ошибкой"
                + (f": {description}" if description else ""),
            )
        return

    result = latest.get("self_test_result", {})
    if not isinstance(result, dict):
        return
    value = get_int(result.get("value"))
    if value in (None, 0):
        return
    description = str(result.get("string", "")).strip()
    _add_issue(
        warning,
        "Последний самотест NVMe завершился неуспешно"
        + (f": {description}" if description else ""),
    )


def apply_temperature_thresholds(
    status: DiskStatus,
    critical: list[str],
    warning: list[str],
) -> None:
    temperature = status.temperature
    if temperature is None:
        return

    critical_limit = status.disk.temperature_critical
    warning_limit = status.disk.temperature_warning

    if critical_limit is not None and temperature >= critical_limit:
        _add_issue(
            critical,
            f"Температура {temperature}°C >= критического порога "
            f"{critical_limit}°C",
        )
    elif warning_limit is not None and temperature >= warning_limit:
        _add_issue(
            warning,
            f"Температура {temperature}°C >= порога предупреждения "
            f"{warning_limit}°C",
        )


def read_ata_status(data: dict[str, Any], status: DiskStatus) -> None:
    status.metrics = {
        "reallocated": get_ata_raw_value(data, 5),
        "pending": get_ata_raw_value(data, 197),
        "uncorrectable": get_ata_raw_value(data, 198),
        "crc": get_ata_raw_value(data, 199),
    }

    critical, warning = smartctl_health_bits(status.smartctl_exit_status)

    smart_passed = data.get("smart_status", {}).get("passed")
    if smart_passed is False:
        _add_issue(critical, "Общая оценка SMART: отказ")
    elif smart_passed is not True:
        status.operational_error = True
        _add_issue(critical, "Общая оценка SMART отсутствует в ответе smartctl")

    pending = status.metrics["pending"]
    uncorrectable = status.metrics["uncorrectable"]
    reallocated = status.metrics["reallocated"]
    crc = status.metrics["crc"]

    if pending is not None and pending > 0:
        _add_issue(critical, f"Current_Pending_Sector={pending}")
    if uncorrectable is not None and uncorrectable > 0:
        _add_issue(critical, f"Offline_Uncorrectable={uncorrectable}")
    if reallocated is not None and reallocated > 0:
        _add_issue(warning, f"Reallocated_Sector_Ct={reallocated}")
    if crc is not None and crc > 0:
        _add_issue(warning, f"UDMA_CRC_Error_Count={crc}")

    if "ata_smart_attributes" not in data:
        status.operational_error = True
        _add_issue(critical, "SMART attributes отсутствуют в ответе smartctl")

    apply_latest_selftest_health(status, data, critical, warning)
    apply_temperature_thresholds(status, critical, warning)
    apply_health_result(status, critical, warning)


def read_nvme_status(data: dict[str, Any], status: DiskStatus) -> None:
    health = data.get("nvme_smart_health_information_log")
    critical, warning = smartctl_health_bits(status.smartctl_exit_status)

    if not isinstance(health, dict):
        health = {}
        status.operational_error = True
        _add_issue(critical, "NVMe SMART Health Information Log отсутствует")

    status.metrics = {
        "critical_warning": get_int(health.get("critical_warning")),
        "percentage_used": get_int(health.get("percentage_used")),
        "available_spare": get_int(health.get("available_spare")),
        "media_errors": get_int(health.get("media_errors")),
        "error_entries": get_int(health.get("num_err_log_entries")),
    }

    smart_passed = data.get("smart_status", {}).get("passed")
    if smart_passed is False:
        _add_issue(critical, "Общая оценка SMART: отказ")
    elif smart_passed is not True:
        status.operational_error = True
        _add_issue(critical, "Общая оценка SMART отсутствует в ответе smartctl")

    critical_warning = status.metrics["critical_warning"]
    percentage_used = status.metrics["percentage_used"]
    media_errors = status.metrics["media_errors"]
    error_entries = status.metrics["error_entries"]

    if critical_warning is not None and critical_warning != 0:
        _add_issue(critical, f"Critical Warning=0x{critical_warning:02x}")
    if media_errors is not None and media_errors > 0:
        _add_issue(critical, f"Media and Data Integrity Errors={media_errors}")
    if error_entries is not None and error_entries > 0:
        _add_issue(warning, f"Error Information Log Entries={error_entries}")

    wear_critical = status.disk.wear_critical_percent
    wear_warning = status.disk.wear_warning_percent
    if (
        percentage_used is not None
        and wear_critical is not None
        and percentage_used >= wear_critical
    ):
        _add_issue(
            critical,
            f"Износ NVMe {percentage_used}% >= критического порога "
            f"{wear_critical}%",
        )
    elif (
        percentage_used is not None
        and wear_warning is not None
        and percentage_used >= wear_warning
    ):
        _add_issue(
            warning,
            f"Износ NVMe {percentage_used}% >= порога предупреждения "
            f"{wear_warning}%",
        )

    apply_latest_selftest_health(status, data, critical, warning)
    apply_temperature_thresholds(status, critical, warning)
    apply_health_result(status, critical, warning)


def apply_health_result(
    status: DiskStatus,
    critical: list[str],
    warning: list[str],
) -> None:
    if critical:
        status.severity = 2
        status.status_text = "Плохо"
        status.issues = critical + warning
    elif warning:
        status.severity = 1
        status.status_text = "Тревога"
        status.issues = warning
    else:
        status.severity = 0
        status.status_text = "Хорошо"
        status.issues = []


def read_disk_status(disk: Disk, general: dict[str, Any]) -> DiskStatus:
    status = DiskStatus(disk=disk)

    try:
        data, exit_status = run_smartctl_json(disk, general, "-a")
    except MonitorError as exc:
        status.operational_error = True
        status.issues.append(str(exc))
        return status

    status.present = True
    status.smartctl_exit_status = exit_status
    status.temperature = get_temperature(data)
    status.power_on_hours = get_power_on_hours(data)

    if disk.kind == "auto":
        detected = infer_disk_kind(data)
        if detected is None:
            status.operational_error = True
            status.severity = 2
            status.status_text = "Плохо"
            status.issues.append(
                "не удалось определить тип накопителя; "
                "задайте kind = 'ata' или kind = 'nvme' вручную"
            )
            return status
        try:
            validate_detected_kind(disk, detected)
        except MonitorError as exc:
            status.operational_error = True
            status.severity = 2
            status.status_text = "Плохо"
            status.issues.append(str(exc))
            return status
        disk = replace(disk, kind=detected)
        status.disk = disk

    command_error_bits = exit_status & SMARTCTL_COMMAND_ERROR_MASK
    if command_error_bits:
        status.operational_error = True
        messages = smartctl_messages(data)
        status.issues.append(
            f"ошибка команды smartctl, bits=0x{command_error_bits:02x}"
            + (f": {messages}" if messages else "")
        )

    if disk.kind == "ata":
        read_ata_status(data, status)
    else:
        read_nvme_status(data, status)

    if command_error_bits:
        status.severity = 2
        status.status_text = "Плохо"
        issue = (
            f"ошибка команды smartctl, bits=0x{command_error_bits:02x}"
            + (f": {smartctl_messages(data)}" if smartctl_messages(data) else "")
        )
        _add_issue(status.issues, issue)

    return status


# ---------------------------------------------------------------------------
# Самотестирование накопителей
# ---------------------------------------------------------------------------


def selftest_table(disk: Disk, data: dict[str, Any]) -> list[dict[str, Any]]:
    if disk.kind == "ata":
        table = data.get("ata_smart_self_test_log", {}).get("standard", {}).get("table", [])
    else:
        table = data.get("nvme_self_test_log", {}).get("table", [])

    if not isinstance(table, list):
        return []
    return [item for item in table if isinstance(item, dict)]


def selftest_log_signature(disk: Disk, data: dict[str, Any]) -> str:
    # Сравниваем всю таблицу, а не только первую запись. ATA хранит время
    # self-test с точностью до часа, поэтому два одинаковых коротких теста,
    # завершившихся в один Power_On_Hour, могут иметь полностью одинаковую
    # первую запись. При добавлении нового результата сдвигается вся таблица.
    table = selftest_table(disk, data)
    return json.dumps(table, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def read_selftest_log(disk: Disk, general: dict[str, Any]) -> dict[str, Any]:
    data, exit_status = run_smartctl_json(disk, general, "-l", "selftest")
    if exit_status & SMARTCTL_COMMAND_ERROR_MASK:
        messages = smartctl_messages(data)
        raise MonitorError(
            f"{disk.label}: не удалось прочитать self-test log "
            f"(smartctl bits=0x{exit_status & SMARTCTL_COMMAND_ERROR_MASK:02x})"
            + (f": {messages}" if messages else "")
        )
    return data


def current_selftest_state(disk: Disk, general: dict[str, Any]) -> SelfTestState:
    if disk.kind == "ata":
        data, exit_status = run_smartctl_json(disk, general, "-a")
        if exit_status & SMARTCTL_COMMAND_ERROR_MASK:
            raise MonitorError(
                f"{disk.label}: не удалось прочитать состояние self-test "
                f"(smartctl bits=0x{exit_status & SMARTCTL_COMMAND_ERROR_MASK:02x})"
            )

        status = data.get("ata_smart_data", {}).get("self_test", {}).get("status", {})
        if not isinstance(status, dict):
            raise MonitorError(f"{disk.label}: smartctl не вернул ATA self-test status")

        value = get_int(status.get("value"))
        description = str(status.get("string", "")).strip()

        if value is not None:
            in_progress = (value >> 4) == 0x0F
            remaining = (value & 0x0F) * 10 if in_progress else None
        else:
            # Запасной вариант для необычного или старого JSON-вывода smartctl.
            in_progress = "in progress" in description.lower()
            remaining = None

        return SelfTestState(
            in_progress=in_progress,
            description=description,
            remaining_percent=remaining,
            data=data,
        )

    data = read_selftest_log(disk, general)
    operation = data.get("nvme_self_test_log", {}).get("current_self_test_operation", {})
    if not isinstance(operation, dict):
        raise MonitorError(f"{disk.label}: smartctl не вернул NVMe self-test operation")

    value = get_int(operation.get("value"))
    description = str(operation.get("string", "")).strip()
    in_progress = value is not None and value != 0

    completion = data.get("nvme_self_test_log", {}).get("current_self_test_completion_percent")
    completion_value = get_int(completion)
    remaining = None
    if in_progress and completion_value is not None:
        remaining = max(0, min(100, 100 - completion_value))

    return SelfTestState(
        in_progress=in_progress,
        description=description,
        remaining_percent=remaining,
        data=data,
    )


def selftest_timeout_minutes(
    disk: Disk,
    test_type: str,
    capability_data: dict[str, Any],
    general: dict[str, Any],
) -> tuple[int, int | None]:
    if disk.kind == "ata":
        polling = (
            capability_data
            .get("ata_smart_data", {})
            .get("self_test", {})
            .get("polling_minutes", {})
        )
        if not isinstance(polling, dict):
            polling = {}

        key = "short" if test_type == "short" else "extended"
        recommended = get_int(polling.get(key))

        if recommended is not None and recommended > 0:
            grace_key = (
                "ata_short_grace_minutes"
                if test_type == "short"
                else "ata_long_grace_minutes"
            )
            default_grace = 10 if test_type == "short" else 60
            grace = _get_int(general, grace_key, default_grace, 1)
            return recommended + grace, recommended

        fallback = 20 if test_type == "short" else 300
        return fallback, None

    key = "nvme_short_timeout_minutes" if test_type == "short" else "nvme_long_timeout_minutes"
    default = 30 if test_type == "short" else 360
    return _get_int(general, key, default, 1), None


def evaluate_selftest_result(
    disk: Disk,
    test_type: str,
    data: dict[str, Any],
    result: SelfTestResult,
) -> None:
    table = selftest_table(disk, data)
    if not table:
        result.outcome = SelfTestOutcome.RESULT_MISSING
        result.detail = "новая запись self-test отсутствует"
        return

    latest = table[0]

    if disk.kind == "ata":
        type_text = str(latest.get("type", {}).get("string", "")).lower()
        expected = "short" in type_text if test_type == "short" else "extended" in type_text
        if not expected:
            result.outcome = SelfTestOutcome.RESULT_MISSING
            result.detail = "последняя запись журнала относится к другому типу теста"
            return

        status = latest.get("status", {})
        if not isinstance(status, dict):
            result.outcome = SelfTestOutcome.RESULT_MISSING
            result.detail = "в журнале самотестов отсутствует status"
            return

        passed = status.get("passed")
        value = get_int(status.get("value"))
        description = str(status.get("string", "неизвестный результат")).strip()
        lowered = description.lower()
        status_code = ((value >> 4) & 0x0F) if value is not None else None
        result.completed = True

        if (
            passed is True
            or status_code == 0
            or "completed without error" in lowered
        ):
            result.outcome = SelfTestOutcome.PASSED
            result.detail = "успешно"
        else:
            result.outcome = SelfTestOutcome.FAILED
            result.detail = description or "тест завершился с ошибкой"
        return

    type_text = str(latest.get("self_test_code", {}).get("string", "")).lower()
    expected = "short" in type_text if test_type == "short" else "extended" in type_text
    if not expected:
        result.outcome = SelfTestOutcome.RESULT_MISSING
        result.detail = "последняя запись журнала относится к другому типу теста"
        return

    selftest_result = latest.get("self_test_result", {})
    if not isinstance(selftest_result, dict):
        result.outcome = SelfTestOutcome.RESULT_MISSING
        result.detail = "в журнале самотестов NVMe отсутствует result"
        return

    value = get_int(selftest_result.get("value"))
    description = str(selftest_result.get("string", "неизвестный результат")).strip()
    result.completed = True

    if value == 0:
        result.outcome = SelfTestOutcome.PASSED
        result.detail = "успешно"
    else:
        result.outcome = SelfTestOutcome.FAILED
        result.detail = description or "тест завершился с ошибкой"


def abort_selftest(
    disk: Disk,
    general: dict[str, Any],
    *,
    verify: bool = True,
) -> tuple[bool, str]:
    try:
        command = run_smartctl_raw(disk, general, "-X")
    except MonitorError as exc:
        return False, str(exc)

    command_error_bits = command.returncode & SMARTCTL_COMMAND_ERROR_MASK
    output = " | ".join(
        part.strip().replace("\n", " | ")
        for part in (command.stdout, command.stderr)
        if part.strip()
    )

    if command_error_bits:
        detail = f"smartctl -X rc={command.returncode}"
        if output:
            detail += f": {output}"
        return False, detail

    if not verify:
        return True, output

    verify_seconds = _get_int(general, "abort_verify_seconds", 30, 1)
    deadline = time.monotonic() + verify_seconds
    last_error = ""

    while time.monotonic() < deadline:
        try:
            state = current_selftest_state(disk, general)
            if not state.in_progress:
                return True, state.description or output
        except MonitorError as exc:
            last_error = str(exc)
        time.sleep(1)

    return False, last_error or "self-test остаётся активным после smartctl -X"


def _abort_active_test(
    active: ActiveTest,
    general: dict[str, Any],
    reason: str,
    *,
    known_in_progress: bool = False,
) -> bool:
    result = active.result
    if not result.owned:
        return True

    # При обычной обработке таймаута или ошибки вызывающий код только что
    # видел активный тест либо потерял возможность проверить его состояние.
    # В аварийной очистке сначала перечитываем состояние, чтобы не остановить
    # уже завершившийся между двумя проверками тест.
    if not known_in_progress:
        try:
            state = current_selftest_state(result.disk, general)
            if not state.in_progress:
                result.abort_succeeded = True
                return True
        except MonitorError:
            pass

    result.abort_attempted = True
    ok, detail = abort_selftest(result.disk, general)
    result.abort_succeeded = ok

    if ok:
        LOG.warning("%s: self-test остановлен (%s)", result.disk.label, reason)
    else:
        LOG.error("%s: не удалось остановить self-test (%s): %s", result.disk.label, reason, detail)

    return ok


def run_selftests(
    disks: list[Disk],
    test_type: str,
    general: dict[str, Any],
) -> dict[str, SelfTestResult]:
    results: dict[str, SelfTestResult] = {}
    active: dict[str, ActiveTest] = {}
    pending: set[str] = set()

    poll_interval = _get_int(general, "poll_interval_seconds", 20, 5)
    poll_error_limit = _get_int(general, "poll_error_limit", 3, 1)
    settle_seconds = _get_int(general, "selftest_result_settle_seconds", 30, 1)

    LOG.info("Запуск %s self-test", test_type)

    try:
        # Запуск тестов находится внутри области аварийной очистки. Если после
        # старта одного диска возникнет неожиданное исключение, уже запущенный
        # нами тест будет остановлен в finally и не останется без контроля.
        for disk in disks:
            result = SelfTestResult(disk=disk, test_type=test_type)
            results[disk.path] = result

            try:
                if disk.kind == "auto":
                    raise MonitorError(
                        f"{disk.label}: тип накопителя не определён; "
                        "самотест не запускается"
                    )

                before_log = read_selftest_log(disk, general)
                before_signature = selftest_log_signature(disk, before_log)
                state = current_selftest_state(disk, general)

                if state.in_progress:
                    result.outcome = SelfTestOutcome.BUSY
                    result.detail = "уже выполняется другой self-test"
                    result.remaining_percent = state.remaining_percent
                    if state.description:
                        result.detail += f" ({state.description})"
                    LOG.warning("%s: %s", disk.label, result.detail)
                    continue

                timeout_minutes, recommended = selftest_timeout_minutes(
                    disk,
                    test_type,
                    state.data,
                    general,
                )
                result.timeout_minutes = timeout_minutes
                result.recommended_minutes = recommended

                command = run_smartctl_raw(disk, general, "-t", test_type)
                command_error_bits = command.returncode & SMARTCTL_COMMAND_ERROR_MASK
                output = " | ".join(
                    part.strip().replace("\n", " | ")
                    for part in (command.stdout, command.stderr)
                    if part.strip()
                )

                if command_error_bits:
                    result.outcome = SelfTestOutcome.START_FAILED
                    result.detail = (
                        f"не удалось запустить self-test (smartctl rc={command.returncode})"
                    )
                    if output:
                        result.detail += f": {output}"
                    LOG.error("%s: %s", disk.label, result.detail)
                    continue

                result.started = True
                result.owned = True
                active[disk.path] = ActiveTest(
                    result=result,
                    before_signature=before_signature,
                    deadline=time.monotonic() + timeout_minutes * 60,
                )
                pending.add(disk.path)

                extra = f", recommended={recommended} мин" if recommended is not None else ""
                LOG.info(
                    "%s: %s self-test запущен, timeout=%d мин%s",
                    disk.label,
                    test_type,
                    timeout_minutes,
                    extra,
                )

            except MonitorError as exc:
                result.outcome = SelfTestOutcome.START_FAILED
                result.detail = str(exc)
                LOG.error("%s", exc)

        while pending:
            if _STOP_REQUESTED:
                for disk_path in list(pending):
                    item = active[disk_path]
                    result = item.result
                    stopped = _abort_active_test(item, general, "завершение процесса")
                    result.outcome = SelfTestOutcome.INTERRUPTED
                    result.detail = "мониторинг прерван сигналом"
                    if result.owned:
                        result.detail += (
                            "; тест остановлен"
                            if stopped
                            else "; остановить тест не удалось"
                        )
                    pending.remove(disk_path)
                break

            now = time.monotonic()

            for disk_path in list(pending):
                item = active[disk_path]
                result = item.result
                disk = result.disk

                try:
                    # Сначала опрашиваем диск и только затем проверяем срок ожидания.
                    # Так тест, завершившийся ровно на границе срока, не будет
                    # ошибочно помечен как зависший.
                    state = current_selftest_state(disk, general)
                    item.poll_errors = 0
                    item.last_error = ""
                    result.remaining_percent = state.remaining_percent

                    if state.in_progress:
                        item.inactive_since = None

                        if now < item.deadline:
                            LOG.debug(
                                "%s: self-test выполняется%s%s",
                                disk.label,
                                f" ({state.description})" if state.description else "",
                                (
                                    f", осталось {state.remaining_percent}%"
                                    if state.remaining_percent is not None
                                    else ""
                                ),
                            )
                            continue

                        timeout_detail = f"тест не завершился за {result.timeout_minutes} мин"
                        if state.remaining_percent is not None:
                            timeout_detail += f", осталось {state.remaining_percent}%"

                        stopped = _abort_active_test(
                            item,
                            general,
                            "timeout",
                            known_in_progress=True,
                        )
                        if stopped:
                            result.outcome = SelfTestOutcome.TIMEOUT_ABORTED
                            result.detail = timeout_detail + "; тест остановлен"
                        else:
                            result.outcome = SelfTestOutcome.TIMEOUT_ABORT_FAILED
                            result.detail = timeout_detail + "; остановить тест не удалось"

                        LOG.error("%s: %s", disk.label, result.detail)
                        pending.remove(disk_path)
                        continue

                    # Диск сообщает, что тест уже не активен. Даём журналу короткое
                    # время на появление новой записи и не ждём исходный
                    # многочасовой срок теста.
                    if item.inactive_since is None:
                        item.inactive_since = now

                    after_log = (
                        state.data
                        if disk.kind == "nvme"
                        else read_selftest_log(disk, general)
                    )
                    after_signature = selftest_log_signature(disk, after_log)

                    if after_signature == item.before_signature:
                        if now - item.inactive_since < settle_seconds:
                            LOG.debug(
                                "%s: тест уже не активен, ждём свежую запись журнала самотестов",
                                disk.label,
                            )
                            continue

                        result.outcome = SelfTestOutcome.RESULT_MISSING
                        result.detail = (
                            "тест завершился, но новая запись журнала самотестов не появилась "
                            f"за {settle_seconds} сек"
                        )
                        LOG.error("%s: %s", disk.label, result.detail)
                        pending.remove(disk_path)
                        continue

                    evaluate_selftest_result(disk, test_type, after_log, result)
                    if result.passed:
                        LOG.info("%s: %s self-test завершён успешно", disk.label, test_type)
                    else:
                        LOG.error("%s: %s self-test: %s", disk.label, test_type, result.detail)
                    pending.remove(disk_path)

                except MonitorError as exc:
                    item.poll_errors += 1
                    item.last_error = str(exc)
                    LOG.warning(
                        "%s: ошибка опроса self-test (%d/%d): %s",
                        disk.label,
                        item.poll_errors,
                        poll_error_limit,
                        exc,
                    )

                    if item.poll_errors < poll_error_limit:
                        continue

                    stopped = _abort_active_test(
                        item,
                        general,
                        "ошибка контроля",
                        known_in_progress=True,
                    )

                    result.outcome = SelfTestOutcome.MONITOR_FAILED
                    result.detail = "не удалось контролировать выполнение теста: " + str(exc)
                    result.detail += (
                        "; тест остановлен"
                        if stopped
                        else "; остановить тест не удалось"
                    )
                    pending.remove(disk_path)

            if pending:
                time.sleep(poll_interval)

    finally:
        # Неожиданная ошибка Python после успешного smartctl -t не должна
        # оставлять запущенный нами тест без контроля. В аварийную очистку
        # попадают только тесты без окончательного результата.
        for item in active.values():
            result = item.result
            if result.owned and result.outcome == SelfTestOutcome.NOT_RUN:
                stopped = _abort_active_test(item, general, "аварийное завершение мониторинга")
                result.outcome = SelfTestOutcome.INTERRUPTED
                result.detail = "мониторинг аварийно завершён"
                result.detail += "; тест остановлен" if stopped else "; остановить тест не удалось"

    return results


# ---------------------------------------------------------------------------
# Формирование отчётов
# ---------------------------------------------------------------------------


def status_marker(severity: int) -> str:
    if severity >= 2:
        return "[ОШИБКА]"
    if severity == 1:
        return "[ВНИМАНИЕ]"
    return "[НОРМА]"


def format_value(value: int | None) -> str:
    return "—" if value is None else str(value)


def selftest_severity(result: SelfTestResult) -> int:
    if result.outcome == SelfTestOutcome.PASSED:
        return 0

    if result.outcome in {
        SelfTestOutcome.TIMEOUT_ABORTED,
        SelfTestOutcome.BUSY,
    }:
        return 1

    return 2


def selftest_line(result: SelfTestResult) -> str:
    name = "Короткий тест" if result.test_type == "short" else "Длительный тест"

    if result.outcome == SelfTestOutcome.PASSED:
        return f"{name}: успешно"
    if result.outcome == SelfTestOutcome.TIMEOUT_ABORTED:
        return f"{name}: превышено время — {result.detail}"
    if result.outcome == SelfTestOutcome.BUSY:
        return f"{name}: не запущен — {result.detail}"
    if result.outcome == SelfTestOutcome.START_FAILED:
        return f"{name}: ошибка запуска — {result.detail}"
    if result.outcome == SelfTestOutcome.MONITOR_FAILED:
        return f"{name}: ошибка контроля — {result.detail}"
    if result.outcome == SelfTestOutcome.TIMEOUT_ABORT_FAILED:
        return f"{name}: критическая ошибка контроля — {result.detail}"
    if result.outcome == SelfTestOutcome.INTERRUPTED:
        return f"{name}: прерван — {result.detail}"
    if result.outcome == SelfTestOutcome.RESULT_MISSING:
        return f"{name}: результат не подтверждён — {result.detail}"
    return f"{name}: ошибка — {result.detail or 'неизвестный результат'}"


def build_report(
    hostname: str,
    statuses: list[DiskStatus],
    test_results: dict[str, SelfTestResult] | None = None,
) -> str:
    now = time.strftime("%d.%m.%Y %H:%M")
    lines = ["SMART Monitor", f"Сервер: {hostname} | {now}"]

    for status in statuses:
        lines.append("")

        result = (
            test_results.get(status.disk.path)
            if test_results is not None
            else None
        )
        effective_severity = status.severity
        if result is not None:
            effective_severity = max(
                effective_severity,
                selftest_severity(result),
            )

        lines.append(
            f"{status_marker(effective_severity)} {status.disk.label}"
        )

        if result is not None:
            lines.append(selftest_line(result))

        metrics = status.metrics
        lines.append(f"Температура: {format_value(status.temperature)}°C")

        if status.disk.kind == "ata":
            lines.append(f"Переназначено: {format_value(metrics.get('reallocated'))}")
            lines.append(f"Ожидают переназначения: {format_value(metrics.get('pending'))}")
            lines.append(f"Неисправимых: {format_value(metrics.get('uncorrectable'))}")
            lines.append(f"CRC-ошибок: {format_value(metrics.get('crc'))}")
        elif status.disk.kind == "nvme":
            lines.append(f"Износ: {format_value(metrics.get('percentage_used'))}%")
            lines.append(f"Резерв: {format_value(metrics.get('available_spare'))}%")
            lines.append(f"Ошибок носителя: {format_value(metrics.get('media_errors'))}")

        if status.issues:
            lines.append("Причина: " + "; ".join(status.issues))

    lines.append("")
    problem_disks: set[str] = {
        status.disk.path for status in statuses if status.severity != 0 or status.operational_error
    }

    if test_results is not None:
        for disk_path, result in test_results.items():
            if result.outcome != SelfTestOutcome.PASSED:
                problem_disks.add(disk_path)

    if not problem_disks:
        lines.append("Все накопители исправны")
    else:
        lines.append(f"Требуют внимания: {len(problem_disks)} накопител.")

    return "\n".join(lines)


def sms_value(value: int | None) -> str:
    return "-" if value is None else str(value)


def sms_selftest_reason(result: SelfTestResult) -> str:
    mapping = {
        SelfTestOutcome.FAILED: "тест: ошибка",
        SelfTestOutcome.TIMEOUT_ABORTED: "тест: превышено время",
        SelfTestOutcome.TIMEOUT_ABORT_FAILED: "тест: не удалось остановить",
        SelfTestOutcome.BUSY: "тест: уже выполняется",
        SelfTestOutcome.START_FAILED: "тест: не запущен",
        SelfTestOutcome.MONITOR_FAILED: "тест: потерян контроль",
        SelfTestOutcome.INTERRUPTED: "тест: прерван",
        SelfTestOutcome.RESULT_MISSING: "тест: нет результата",
    }
    return mapping.get(result.outcome, "тест: проблема")


def build_sms_summary(
    hostname: str,
    statuses: list[DiskStatus],
    test_results: dict[str, SelfTestResult] | None = None,
) -> str:
    problem_disks: list[tuple[DiskStatus, SelfTestResult | None]] = []

    for status in statuses:
        result = test_results.get(status.disk.path) if test_results is not None else None
        if (
            status.severity != 0
            or status.operational_error
            or (result is not None and result.outcome != SelfTestOutcome.PASSED)
        ):
            problem_disks.append((status, result))

    if not problem_disks:
        lines = [hostname]
        ata_index = 0
        for status in statuses:
            if status.disk.kind == "ata":
                ata_index += 1
                lines.append(f"HDD{ata_index}: {sms_value(status.temperature)}C, SMART: норма")
            elif status.disk.kind == "nvme":
                metrics = status.metrics
                lines.append(
                    f"NVMe: {sms_value(status.temperature)}C, "
                    f"износ {sms_value(metrics.get('percentage_used'))}%, "
                    f"ошибки {sms_value(metrics.get('media_errors'))}"
                )
            else:
                lines.append(f"{status.disk.label}: SMART: норма")
        return "\n".join(lines)

    lines = [f"{hostname} SMART: ПРОБЛЕМА"]
    ata_index = 0
    nvme_index = 0
    disk_names: dict[str, str] = {}

    for status in statuses:
        if status.disk.kind == "ata":
            ata_index += 1
            disk_names[status.disk.path] = f"HDD{ata_index}"
        elif status.disk.kind == "nvme":
            nvme_index += 1
            disk_names[status.disk.path] = "NVMe" if nvme_index == 1 else f"NVMe{nvme_index}"
        else:
            disk_names[status.disk.path] = status.disk.label

    for status, result in problem_disks:
        reasons: list[str] = []
        if result is not None and result.outcome != SelfTestOutcome.PASSED:
            reasons.append(sms_selftest_reason(result))

        temperature = status.temperature
        if (
            temperature is not None
            and status.disk.temperature_warning is not None
            and temperature >= status.disk.temperature_warning
        ):
            reasons.append(f"температура {temperature}C")

        if status.disk.kind == "ata":
            metrics = status.metrics
            if metrics.get("pending"):
                reasons.append(f"ожидают {metrics['pending']}")
            if metrics.get("uncorrectable"):
                reasons.append(f"неисправимых {metrics['uncorrectable']}")
            if metrics.get("reallocated"):
                reasons.append(f"переназначено {metrics['reallocated']}")
            if metrics.get("crc"):
                reasons.append(f"CRC {metrics['crc']}")
        elif status.disk.kind == "nvme":
            metrics = status.metrics
            if metrics.get("media_errors"):
                reasons.append(f"ошибок носителя {metrics['media_errors']}")
            wear = metrics.get("percentage_used")
            if (
                wear is not None
                and status.disk.wear_warning_percent is not None
                and wear >= status.disk.wear_warning_percent
            ):
                reasons.append(f"износ {wear}%")
            if metrics.get("critical_warning"):
                reasons.append(f"критическое предупреждение {metrics['critical_warning']}")

        if status.operational_error:
            reasons.append("ошибка мониторинга")
        if not reasons and status.issues:
            reasons.append(status.issues[0][:80])
        if not reasons:
            reasons.append("неизвестная проблема")

        name = disk_names.get(status.disk.path, status.disk.label)
        lines.append(f"{name}: " + ", ".join(reasons))

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Каналы уведомлений по HTTP
# ---------------------------------------------------------------------------


def direct_opener() -> urllib.request.OpenerDirector:
    return urllib.request.build_opener(urllib.request.ProxyHandler({}))


def proxy_url_with_auth(proxy_url: str, username: str, password: str) -> str:
    if not username:
        return proxy_url

    parsed = urllib.parse.urlsplit(proxy_url)
    if not parsed.scheme or not parsed.hostname:
        raise MonitorError("Некорректный Telegram proxy_url")

    user = urllib.parse.quote(username, safe="")
    passwd = urllib.parse.quote(password, safe="")
    host = parsed.hostname
    if ":" in host and not host.startswith("["):
        host = f"[{host}]"

    netloc = f"{user}:{passwd}@{host}"
    if parsed.port is not None:
        netloc += f":{parsed.port}"

    return urllib.parse.urlunsplit(
        (parsed.scheme, netloc, parsed.path, parsed.query, parsed.fragment)
    )


def telegram_opener(telegram: dict[str, Any]) -> urllib.request.OpenerDirector:
    if not telegram.get("proxy_enabled", False):
        return direct_opener()

    proxy_url = str(telegram.get("proxy_url", "")).strip()
    if not proxy_url:
        raise MonitorError("Telegram proxy включён, но proxy_url пуст")

    proxy_url = proxy_url_with_auth(
        proxy_url,
        str(telegram.get("proxy_username", "")),
        secret_value(telegram, "proxy_password"),
    )
    return urllib.request.build_opener(
        urllib.request.ProxyHandler({"http": proxy_url, "https": proxy_url})
    )


def read_limited(response: Any, limit: int = 1_048_576) -> bytes:
    data = response.read(limit + 1)
    if len(data) > limit:
        raise MonitorError("HTTP-ответ превышает допустимый размер")
    return data


def split_message(message: str, limit: int) -> list[str]:
    """Разбить длинное сообщение, по возможности сохраняя целые строки."""
    if limit < 1:
        raise ValueError("limit должен быть положительным")
    if not message:
        return [""]
    if len(message) <= limit:
        return [message]

    chunks: list[str] = []
    current = ""

    for line in message.splitlines(keepends=True):
        while len(line) > limit:
            if current:
                chunks.append(current)
                current = ""
            chunks.append(line[:limit])
            line = line[limit:]

        if len(current) + len(line) > limit:
            if current:
                chunks.append(current)
            current = line
        else:
            current += line

    if current:
        chunks.append(current)

    # splitlines(keepends=True) сохраняет исходный текст. Эта проверка защищает
    # от будущих изменений функции, которые могли бы незаметно потерять данные.
    if "".join(chunks) != message:
        raise RuntimeError("внутренняя ошибка разбиения сообщения")

    return chunks


def send_telegram(config: dict[str, Any], message: str) -> ChannelAttempt:
    telegram = config.get("telegram", {})
    if not isinstance(telegram, dict) or not telegram.get("enabled", False):
        return ChannelAttempt("telegram", False, None, "отключён")

    token = secret_value(telegram, "token")
    chat_id = str(telegram.get("chat_id", "")).strip()
    if not token or not chat_id:
        return ChannelAttempt("telegram", True, False, "token/chat_id не заполнены")

    try:
        opener = telegram_opener(telegram)
        url = f"https://api.telegram.org/bot{token}/sendMessage"
        chunks = split_message(message, 4000)

        for number, chunk in enumerate(chunks, start=1):
            body = urllib.parse.urlencode({"chat_id": chat_id, "text": chunk}).encode("utf-8")
            request = urllib.request.Request(url, data=body, method="POST")
            with opener.open(request, timeout=15) as response:
                raw = read_limited(response)
            payload = json.loads(raw.decode("utf-8", errors="replace"))
            if not isinstance(payload, dict) or payload.get("ok") is not True:
                return ChannelAttempt(
                    "telegram",
                    True,
                    False,
                    f"API не подтвердил отправку части {number}/{len(chunks)}",
                )

        suffix = " через proxy" if telegram.get("proxy_enabled", False) else ""
        parts = f", частей: {len(chunks)}" if len(chunks) > 1 else ""
        LOG.info("Отчёт отправлен в Telegram%s%s", suffix, parts)
        return ChannelAttempt("telegram", True, True)
    except (urllib.error.URLError, TimeoutError, OSError, ValueError, MonitorError) as exc:
        LOG.error("Ошибка Telegram: %s", exc)
        return ChannelAttempt("telegram", True, False, str(exc))


def send_matrix(config: dict[str, Any], message: str) -> ChannelAttempt:
    matrix = config.get("matrix", {})
    if not isinstance(matrix, dict) or not matrix.get("enabled", False):
        return ChannelAttempt("matrix", False, None, "отключён")

    base_url = str(matrix.get("url", "")).strip().rstrip("/")
    room_id = str(matrix.get("room_id", "")).strip()
    token = secret_value(matrix, "token")
    if not base_url or not room_id or not token:
        return ChannelAttempt("matrix", True, False, "параметры заполнены не полностью")

    room_encoded = urllib.parse.quote(room_id, safe="")
    transaction_id = uuid.uuid4().hex
    url = f"{base_url}/_matrix/client/v3/rooms/{room_encoded}/send/m.room.message/{transaction_id}"
    body = json.dumps({"msgtype": "m.text", "body": message}, ensure_ascii=False).encode("utf-8")
    request = urllib.request.Request(
        url,
        data=body,
        method="PUT",
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
    )

    try:
        with direct_opener().open(request, timeout=10) as response:
            raw = read_limited(response)
        payload = json.loads(raw.decode("utf-8", errors="replace"))
        event_id = payload.get("event_id") if isinstance(payload, dict) else None
        if not isinstance(event_id, str) or not event_id:
            return ChannelAttempt(
                "matrix",
                True,
                False,
                "сервер не подтвердил отправку event_id",
            )
        LOG.info("Отчёт отправлен в Matrix")
        return ChannelAttempt("matrix", True, True)
    except (
        urllib.error.URLError,
        TimeoutError,
        OSError,
        ValueError,
        MonitorError,
    ) as exc:
        LOG.error("Ошибка Matrix: %s", exc)
        return ChannelAttempt("matrix", True, False, str(exc))


def send_max(config: dict[str, Any], message: str) -> ChannelAttempt:
    max_config = config.get("max", {})
    if not isinstance(max_config, dict) or not max_config.get("enabled", False):
        return ChannelAttempt("max", False, None, "отключён")

    token = secret_value(max_config, "bot_token")
    chat_id = str(max_config.get("chat_id", "")).strip()
    api_host = str(max_config.get("api_host", "platform-api2.max.ru")).strip()
    api_port = max_config.get("api_port", 443)
    if not isinstance(api_port, int) or isinstance(api_port, bool) or not (1 <= api_port <= 65535):
        api_port = 443

    if not token or not chat_id:
        return ChannelAttempt("max", True, False, "bot_token/chat_id не заполнены")
    if not api_host:
        return ChannelAttempt("max", True, False, "api_host пуст")

    base_url = f"https://{api_host}" + (f":{api_port}" if api_port != 443 else "")
    query = urllib.parse.urlencode({"chat_id": chat_id})
    url = f"{base_url}/messages?{query}"
    try:
        opener = direct_opener()
        chunks = split_message(message, 3900)

        for number, chunk in enumerate(chunks, start=1):
            body = json.dumps({"text": chunk}, ensure_ascii=False).encode("utf-8")
            request = urllib.request.Request(
                url,
                data=body,
                method="POST",
                headers={"Authorization": token, "Content-Type": "application/json"},
            )
            with opener.open(request, timeout=20) as response:
                raw = read_limited(response)
            payload = json.loads(raw.decode("utf-8", errors="replace"))
            confirmed = payload.get("message") if isinstance(payload, dict) else None
            if not isinstance(confirmed, dict):
                return ChannelAttempt(
                    "max",
                    True,
                    False,
                    f"API не подтвердил отправку части {number}/{len(chunks)}",
                )

            # MAX ограничивает частоту отправки в один чат. Небольшая пауза
            # нужна только между частями одного длинного отчёта.
            if number < len(chunks):
                time.sleep(0.55)

        parts = f", частей: {len(chunks)}" if len(chunks) > 1 else ""
        LOG.info("Отчёт отправлен в MAX%s", parts)
        return ChannelAttempt("max", True, True)
    except (urllib.error.URLError, TimeoutError, OSError, ValueError, MonitorError) as exc:
        LOG.error("Ошибка MAX: %s", exc)
        return ChannelAttempt("max", True, False, str(exc))


# ---------------------------------------------------------------------------
# Резервное SMS через модем Huawei
# ---------------------------------------------------------------------------


def modem_request(
    url: str,
    *,
    data: bytes | None = None,
    headers: dict[str, str] | None = None,
    timeout: int = 15,
) -> bytes:
    request = urllib.request.Request(
        url,
        data=data,
        method="POST" if data is not None else "GET",
        headers=headers or {},
    )
    try:
        with direct_opener().open(request, timeout=timeout) as response:
            return read_limited(response, 2_097_152)
    except (urllib.error.URLError, TimeoutError, OSError, MonitorError) as exc:
        raise MonitorError(f"Huawei modem HTTP error: {exc}") from exc


def modem_auth(config: dict[str, Any]) -> tuple[str, str, str]:
    sms = config.get("sms_fallback", {})
    if not isinstance(sms, dict):
        raise MonitorError("Секция [sms_fallback] некорректна")

    modem_url = str(sms.get("modem_url", "")).strip().rstrip("/")
    if not modem_url:
        raise MonitorError("Huawei modem_url не указан")

    raw = modem_request(f"{modem_url}/api/webserver/SesTokInfo", timeout=10)
    try:
        root = ET.fromstring(raw)
    except ET.ParseError as exc:
        raise MonitorError("Huawei: ошибка разбора SesTokInfo") from exc

    token = root.findtext(".//TokInfo", default="").strip()
    cookie = root.findtext(".//SesInfo", default="").strip()
    if not token or not cookie:
        raise MonitorError("Huawei: SesTokInfo не содержит token/cookie")

    return modem_url, token, cookie


def modem_headers(token: str, cookie: str) -> dict[str, str]:
    return {
        "__RequestVerificationToken": token,
        "Cookie": cookie,
        "Content-Type": "text/xml",
    }


def modem_response_ok(raw: bytes) -> bool:
    try:
        root = ET.fromstring(raw)
    except ET.ParseError:
        return False
    return root.tag == "response" and (root.text or "").strip() == "OK"


def list_sent_sms(config: dict[str, Any]) -> list[dict[str, str]]:
    modem_url, token, cookie = modem_auth(config)
    root = ET.Element("request")
    for name, value in (
        ("PageIndex", "1"),
        ("ReadCount", "50"),
        ("BoxType", "2"),
        ("SortType", "0"),
        ("Ascending", "0"),
        ("UnreadPreferred", "0"),
    ):
        ET.SubElement(root, name).text = value

    body = ET.tostring(root, encoding="utf-8", xml_declaration=True)
    raw = modem_request(
        f"{modem_url}/api/sms/sms-list",
        data=body,
        headers=modem_headers(token, cookie),
    )

    try:
        response = ET.fromstring(raw)
    except ET.ParseError as exc:
        raise MonitorError("Huawei: ошибка разбора sms-list") from exc

    messages: list[dict[str, str]] = []
    for item in response.findall(".//Message"):
        index = (item.findtext("Index") or "").strip()
        if not index.isdigit():
            continue
        messages.append(
            {
                "index": index,
                "phone": (item.findtext("Phone") or "").strip(),
                "content": (item.findtext("Content") or "").strip(),
            }
        )
    return messages


def delete_sms_indexes(config: dict[str, Any], indexes: list[str]) -> bool:
    clean_indexes = [value for value in indexes if value.isdigit()]
    if not clean_indexes:
        return True

    modem_url, token, cookie = modem_auth(config)
    root = ET.Element("request")
    for index in clean_indexes:
        ET.SubElement(root, "Index").text = index

    body = ET.tostring(root, encoding="utf-8", xml_declaration=True)
    response = modem_request(
        f"{modem_url}/api/sms/delete-sms",
        data=body,
        headers=modem_headers(token, cookie),
    )
    return modem_response_ok(response)


def send_sms_via_modem(config: dict[str, Any], phone: str, message: str) -> bool:
    modem_url, token, cookie = modem_auth(config)
    root = ET.Element("request")
    ET.SubElement(root, "Index").text = "-1"
    phones = ET.SubElement(root, "Phones")
    ET.SubElement(phones, "Phone").text = phone
    ET.SubElement(root, "Sca").text = ""
    ET.SubElement(root, "Content").text = message
    ET.SubElement(root, "Length").text = str(len(message))
    ET.SubElement(root, "Reserved").text = "1"
    ET.SubElement(root, "Date").text = "-1"

    body = ET.tostring(root, encoding="utf-8", xml_declaration=True, short_empty_elements=False)
    response = modem_request(
        f"{modem_url}/api/sms/send-sms",
        data=body,
        headers=modem_headers(token, cookie),
    )
    return modem_response_ok(response)


def send_sms_fallback(config: dict[str, Any], message: str) -> ChannelAttempt:
    sms = config.get("sms_fallback", {})
    if not isinstance(sms, dict) or not sms.get("enabled", False):
        return ChannelAttempt("sms", False, None, "отключён")

    modem_url = str(sms.get("modem_url", "")).strip().rstrip("/")
    phone = str(sms.get("phone", "")).strip()
    if not modem_url or not phone:
        return ChannelAttempt("sms", True, False, "modem_url/phone не заполнены")

    lock_file = str(sms.get("lock_file", "/run/lock/huawei-modem-api.lock")).strip()
    lock_wait = _get_int(sms, "lock_wait_seconds", 30, 1)

    try:
        lock_handle = acquire_lock(lock_file, wait_seconds=lock_wait)
    except MonitorError as exc:
        LOG.error("Ошибка lock Huawei: %s", exc)
        return ChannelAttempt("sms", True, False, str(exc))

    if lock_handle is None:
        detail = f"Huawei modem lock занят более {lock_wait} сек"
        LOG.error(detail)
        return ChannelAttempt("sms", True, False, detail)

    try:
        cleanup_sent = _get_bool(sms, "cleanup_sent_message", True)
        before_indexes: set[str] = set()

        if cleanup_sent:
            try:
                before_indexes = {item["index"] for item in list_sent_sms(config)}
            except MonitorError as exc:
                LOG.warning("Huawei: не удалось снять список отправленных SMS до отправки: %s", exc)
                cleanup_sent = False

        if not send_sms_via_modem(config, phone, message):
            LOG.error("Huawei: send-sms вернул ошибку")
            return ChannelAttempt("sms", True, False, "send-sms вернул ошибку")

        LOG.info("Резервное SMS отправлено через Huawei")

        if cleanup_sent:
            try:
                after = list_sent_sms(config)
                candidates = [
                    item["index"]
                    for item in after
                    if item["index"] not in before_indexes
                    and item.get("phone") == phone
                    and item.get("content") == message
                ]
                if len(candidates) == 1:
                    target = candidates[0]
                    if delete_sms_indexes(config, [target]):
                        LOG.info("Huawei: удалено отправленное монитором SMS: %s", target)
                    else:
                        LOG.warning("Huawei: не удалось удалить отправленное монитором SMS")
                elif len(candidates) > 1:
                    # Если найдено несколько одинаково подходящих сообщений, ничего
                    # не удаляем. Лучше оставить нашу копию, чем удалить чужую.
                    LOG.warning(
                        "Huawei: найдено несколько подходящих новых SMS (%s); "
                        "точечную очистку пропускаем",
                        ", ".join(candidates),
                    )
                else:
                    LOG.warning(
                        "Huawei: отправленное монитором SMS "
                        "не найдено для точечной очистки"
                    )
            except MonitorError as exc:
                LOG.warning("Huawei: точечная очистка отправленного SMS не выполнена: %s", exc)

        return ChannelAttempt("sms", True, True)

    except MonitorError as exc:
        LOG.error("Ошибка резервного SMS: %s", exc)
        return ChannelAttempt("sms", True, False, str(exc))
    finally:
        lock_handle.close()


# ---------------------------------------------------------------------------
# Правила доставки уведомлений
# ---------------------------------------------------------------------------


def selected_channels(destination: str) -> tuple[bool, bool, bool]:
    telegram = destination in {"telegram", "telegram-matrix", "telegram-max", "all"}
    matrix = destination in {"matrix", "telegram-matrix", "matrix-max", "all"}
    max_channel = destination in {"max", "telegram-max", "matrix-max", "all"}
    return telegram, matrix, max_channel


def notify(
    config: dict[str, Any],
    message: str,
    destination: str,
    *,
    sms_message: str,
    allow_sms_fallback: bool = True,
) -> bool:
    if destination == "none":
        return True
    if destination not in VALID_DESTINATIONS:
        LOG.error("Неизвестный destination=%r", destination)
        return False

    telegram_selected, matrix_selected, max_selected = selected_channels(destination)
    attempts: list[ChannelAttempt] = []

    if telegram_selected:
        attempts.append(send_telegram(config, message))
    if matrix_selected:
        attempts.append(send_matrix(config, message))
    if max_selected:
        attempts.append(send_max(config, message))

    internet_attempts = [
        attempt
        for attempt in attempts
        if attempt.name in {"telegram", "max"} and attempt.enabled
    ]
    internet_ok = any(attempt.success is True for attempt in internet_attempts)

    # Резервное SMS используется, если был включён хотя бы один внешний
    # канал Telegram/MAX и ни один из них не доставил сообщение.
    # Matrix в это решение не входит.
    if allow_sms_fallback and internet_attempts and not internet_ok:
        LOG.warning("Внешние каналы недоступны; используем Huawei SMS fallback")
        attempts.append(send_sms_fallback(config, sms_message))

    successful = [a for a in attempts if a.success is True]
    attempted_enabled = [a for a in attempts if a.enabled]

    for attempt in attempts:
        if attempt.enabled and attempt.success is False:
            detail = f": {attempt.detail}" if attempt.detail else ""
            LOG.warning(
                "Канал %s не доставил уведомление%s",
                attempt.name,
                detail,
            )

    if successful:
        return True

    if not attempted_enabled:
        LOG.error("Ни один выбранный канал уведомлений не включён")
    else:
        LOG.error("Ни один доступный канал не доставил уведомление")
    return False


# ---------------------------------------------------------------------------
# Командная строка и основная логика
# ---------------------------------------------------------------------------


def has_health_finding(statuses: list[DiskStatus]) -> bool:
    return any(status.severity != 0 for status in statuses)


def has_operational_health_error(statuses: list[DiskStatus]) -> bool:
    return any(status.operational_error for status in statuses)


def has_test_finding(results: dict[str, SelfTestResult]) -> bool:
    return any(result.finding for result in results.values())


def has_test_operational_error(results: dict[str, SelfTestResult]) -> bool:
    return any(result.operational_failure for result in results.values())


def parse_arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Монитор состояния HDD, SSD и NVMe",
        add_help=False,
    )
    parser.add_argument(
        "-h",
        "--help",
        action="help",
        help="Показать справку и выйти",
    )
    parser.add_argument(
        "--version",
        action="version",
        version=f"%(prog)s {VERSION}",
        help="Показать версию и выйти",
    )
    parser.add_argument(
        "mode",
        choices=["check", "short", "long"],
        help="Режим работы",
    )
    parser.add_argument(
        "destination",
        nargs="?",
        choices=sorted(VALID_DESTINATIONS),
        default=None,
        help="Канал уведомлений",
    )
    parser.add_argument(
        "--no-sms-fallback",
        action="store_true",
        help="Не использовать резервное SMS. Удобно для ручного тестирования каналов.",
    )
    parser.add_argument(
        "--config",
        default=DEFAULT_CONFIG,
        help="Путь к файлу config.toml",
    )
    parser.add_argument(
        "--notify",
        action="store_true",
        help="Отправить отчёт даже при отсутствии проблем",
    )
    parser.add_argument(
        "--wait-lock",
        action="store_true",
        help=(
            "Ждать освобождения общей блокировки без ограничения времени. "
            "Используется штатными systemd-службами, чтобы пропущенные "
            "Persistent-запуски выполнялись последовательно."
        ),
    )
    parser.add_argument(
        "--debug",
        action="store_true",
        help="Подробный журнал для диагностики",
    )
    return parser.parse_args()


def choose_exit_code(
    *,
    finding: bool,
    operational_error: bool,
    notification_ok: bool,
) -> int:
    if not notification_ok:
        return EXIT_NOTIFICATION_ERROR
    if operational_error:
        return EXIT_RUNTIME_ERROR
    if finding:
        return EXIT_FINDING
    return EXIT_OK


def main() -> int:
    arguments = parse_arguments()
    logging.basicConfig(
        level=logging.DEBUG if arguments.debug else logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )
    install_signal_handlers()

    try:
        config_path = Path(arguments.config)
        config = load_config(config_path)
        disks = load_disks(config)
        general = validate_general(config)

        smartctl = smartctl_binary(general)
        if not Path(smartctl).is_file() or not os.access(smartctl, os.X_OK):
            raise MonitorError(f"smartctl не найден или не исполняемый: {smartctl}")

        lock_file = str(general.get("lock_file", "/run/smart-monitor.lock")).strip()
        if not lock_file:
            raise MonitorError("general.lock_file не может быть пустым")

        lock_wait = (
            None
            if getattr(arguments, "wait_lock", False)
            else _get_int(general, "lock_wait_seconds", 5, 1)
        )
        lock_handle = acquire_lock(lock_file, wait_seconds=lock_wait)
    except MonitorError as exc:
        LOG.error("%s", exc)
        return EXIT_RUNTIME_ERROR

    if lock_handle is None:
        if _STOP_REQUESTED:
            LOG.info("Ожидание общей блокировки прервано сигналом")
        else:
            LOG.info(
                "Другой экземпляр smart-monitor удерживает блокировку; "
                "текущий запуск пропущен"
            )
        return EXIT_BUSY

    try:
        disks = detect_disk_kinds(disks, general)
        hostname = str(general.get("hostname", "")).strip() or socket.gethostname().split(".")[0]
        destination = arguments.destination
        if destination is None:
            destination = str(general.get("default_destination", "none")).strip()

        force_report = arguments.notify
        base_mode = arguments.mode

        if base_mode == "check":
            LOG.info("Старт SMART check")
            statuses = [read_disk_status(disk, general) for disk in disks]
            report = build_report(hostname, statuses)
            print(report)

            finding = has_health_finding(statuses)
            operational_error = has_operational_health_error(statuses)
            notification_ok = True
            if force_report or finding or operational_error:
                notification_ok = notify(
                    config,
                    report,
                    destination,
                    sms_message=build_sms_summary(hostname, statuses),
                    allow_sms_fallback=not arguments.no_sms_fallback,
                )
            else:
                LOG.info("SMART check завершён без проблем; уведомление не требуется")

            LOG.info("SMART check завершён")
            return choose_exit_code(
                finding=finding,
                operational_error=operational_error,
                notification_ok=notification_ok,
            )

        LOG.info("Старт SMART %s", base_mode)
        test_results = run_selftests(disks, base_mode, general)
        statuses = [read_disk_status(disk, general) for disk in disks]
        report = build_report(hostname, statuses, test_results)
        print(report)

        finding = has_health_finding(statuses) or has_test_finding(test_results)
        operational_error = (
            has_operational_health_error(statuses)
            or has_test_operational_error(test_results)
        )

        should_notify = force_report or base_mode == "long" or finding or operational_error
        notification_ok = True

        if should_notify:
            notification_ok = notify(
                config,
                report,
                destination,
                sms_message=build_sms_summary(hostname, statuses, test_results),
                allow_sms_fallback=not arguments.no_sms_fallback,
            )
        else:
            LOG.info("Short self-test завершён без проблем; уведомление не требуется")

        LOG.info("SMART %s завершён", base_mode)
        return choose_exit_code(
            finding=finding,
            operational_error=operational_error,
            notification_ok=notification_ok,
        )

    except MonitorError as exc:
        LOG.error("%s", exc)
        return EXIT_RUNTIME_ERROR
    except Exception:
        LOG.exception("Необработанная ошибка smart-monitor")
        return EXIT_RUNTIME_ERROR
    finally:
        lock_handle.close()


if __name__ == "__main__":
    sys.exit(main())
