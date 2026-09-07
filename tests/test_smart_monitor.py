from __future__ import annotations

import importlib.util
from pathlib import Path
from types import SimpleNamespace

import pytest

MODULE_PATH = Path(__file__).resolve().parents[1] / 'smart_monitor.py'
spec = importlib.util.spec_from_file_location('sm', MODULE_PATH)
sm = importlib.util.module_from_spec(spec)
import sys
sys.modules['sm'] = sm
assert spec.loader is not None
spec.loader.exec_module(sm)


def disk():
    return sm.Disk('/dev/fake', 'Fake HDD', 'ata')


def cp(rc=0, out='', err=''):
    return SimpleNamespace(returncode=rc, stdout=out, stderr=err)


def test_ata_in_progress_uses_numeric_status_not_english(monkeypatch):
    payload = {
        'smartctl': {'exit_status': 0},
        'ata_smart_data': {
            'self_test': {
                'status': {'value': 0xF1, 'string': 'НЕ АНГЛИЙСКАЯ СТРОКА'}
            }
        },
    }
    monkeypatch.setattr(sm, 'run_smartctl_json', lambda *args, **kwargs: (payload, 0))
    state = sm.current_selftest_state(disk(), {})
    assert state.in_progress is True
    assert state.remaining_percent == 10


def test_health_command_bits_fail_closed(monkeypatch):
    payload = {
        'smartctl': {'exit_status': 2, 'messages': [{'string': 'SMART command failed'}]},
        'smart_status': {'passed': True},
        'temperature': {'current': 30},
        'ata_smart_attributes': {'table': []},
    }
    monkeypatch.setattr(sm, 'run_smartctl_json', lambda *args, **kwargs: (payload, 2))
    status = sm.read_disk_status(disk(), {})
    assert status.operational_error is True
    assert status.severity == 2
    assert any('ошибка команды smartctl' in issue for issue in status.issues)


def test_timeout_aborts_owned_test(monkeypatch):
    d = disk()
    before = {'ata_smart_self_test_log': {'standard': {'table': []}}}
    calls = {'state': 0, 'abort': 0}

    monkeypatch.setattr(sm, 'read_selftest_log', lambda *args, **kwargs: before)
    monkeypatch.setattr(sm, 'selftest_timeout_minutes', lambda *args, **kwargs: (0, 169))
    monkeypatch.setattr(sm.time, 'sleep', lambda *_: None)

    def state(*args, **kwargs):
        calls['state'] += 1
        if calls['state'] == 1:
            return sm.SelfTestState(False, 'completed', None, {'ata_smart_data': {'self_test': {}}})
        if calls['state'] == 2:
            return sm.SelfTestState(True, 'in progress', 10, {})
        return sm.SelfTestState(False, 'aborted by host', None, {})

    def raw(_disk, _general, *args):
        if args == ('-X',):
            calls['abort'] += 1
        return cp(0)

    monkeypatch.setattr(sm, 'current_selftest_state', state)
    monkeypatch.setattr(sm, 'run_smartctl_raw', raw)

    result = sm.run_selftests([d], 'long', {})[d.path]
    assert result.outcome == sm.SelfTestOutcome.TIMEOUT_ABORTED
    assert result.abort_attempted is True
    assert result.abort_succeeded is True
    assert calls['abort'] == 1


def test_monitor_failure_aborts_owned_test(monkeypatch):
    d = disk()
    before = {'ata_smart_self_test_log': {'standard': {'table': []}}}
    calls = {'state': 0, 'abort': 0}

    monkeypatch.setattr(sm, 'read_selftest_log', lambda *args, **kwargs: before)
    monkeypatch.setattr(sm, 'selftest_timeout_minutes', lambda *args, **kwargs: (100, 70))
    monkeypatch.setattr(sm.time, 'sleep', lambda *_: None)

    def state(*args, **kwargs):
        calls['state'] += 1
        if calls['state'] == 1:
            return sm.SelfTestState(False, 'completed', None, {'ata_smart_data': {'self_test': {}}})
        if calls['state'] in (2, 3, 4):
            raise sm.MonitorError('transport error')
        return sm.SelfTestState(False, 'aborted', None, {})

    def raw(_disk, _general, *args):
        if args == ('-X',):
            calls['abort'] += 1
        return cp(0)

    monkeypatch.setattr(sm, 'current_selftest_state', state)
    monkeypatch.setattr(sm, 'run_smartctl_raw', raw)

    result = sm.run_selftests([d], 'long', {'poll_error_limit': 3})[d.path]
    assert result.outcome == sm.SelfTestOutcome.MONITOR_FAILED
    assert result.abort_attempted is True
    assert calls['abort'] == 1


def test_busy_is_finding_not_operational_failure(monkeypatch):
    d = disk()
    before = {'ata_smart_self_test_log': {'standard': {'table': []}}}
    monkeypatch.setattr(sm, 'read_selftest_log', lambda *args, **kwargs: before)
    monkeypatch.setattr(
        sm,
        'current_selftest_state',
        lambda *args, **kwargs: sm.SelfTestState(True, 'in progress', 40, {}),
    )
    result = sm.run_selftests([d], 'short', {})[d.path]
    assert result.outcome == sm.SelfTestOutcome.BUSY
    assert result.finding is True
    assert result.operational_failure is False


def test_result_missing_is_operational_failure():
    d = disk()
    result = sm.SelfTestResult(d, 'long')
    sm.evaluate_selftest_result(d, 'long', {'ata_smart_self_test_log': {'standard': {'table': []}}}, result)
    assert result.outcome == sm.SelfTestOutcome.RESULT_MISSING
    assert result.operational_failure is True


def test_notify_matrix_only_disabled_is_failure(monkeypatch):
    assert sm.notify({'matrix': {'enabled': False}}, 'x', 'matrix', sms_message='x') is False


def test_notify_partial_redundant_delivery_is_success(monkeypatch):
    monkeypatch.setattr(sm, 'send_telegram', lambda *a, **k: sm.ChannelAttempt('telegram', True, True))
    monkeypatch.setattr(sm, 'send_matrix', lambda *a, **k: sm.ChannelAttempt('matrix', True, False, 'fail'))
    monkeypatch.setattr(sm, 'send_max', lambda *a, **k: sm.ChannelAttempt('max', True, False, 'fail'))
    monkeypatch.setattr(sm, 'send_sms_fallback', lambda *a, **k: pytest.fail('fallback should not run'))
    assert sm.notify({}, 'x', 'all', sms_message='x') is True


def test_sms_cleanup_deletes_only_own_new_message(monkeypatch, tmp_path):
    lock = tmp_path / 'modem.lock'
    config = {
        'sms_fallback': {
            'enabled': True,
            'modem_url': 'http://modem',
            'phone': '+70000000000',
            'lock_file': str(lock),
            'cleanup_sent_message': True,
        }
    }
    snapshots = [
        [
            {'index': '10', 'phone': '+7111', 'content': 'old'},
            {'index': '11', 'phone': '+70000000000', 'content': 'older monitor'},
        ],
        [
            {'index': '12', 'phone': '+70000000000', 'content': 'ALERT'},
            {'index': '13', 'phone': '+7222', 'content': 'other new'},
            {'index': '10', 'phone': '+7111', 'content': 'old'},
        ],
    ]
    deleted = []

    monkeypatch.setattr(sm, 'list_sent_sms', lambda *a, **k: snapshots.pop(0))
    monkeypatch.setattr(sm, 'send_sms_via_modem', lambda *a, **k: True)
    monkeypatch.setattr(sm, 'delete_sms_indexes', lambda _cfg, indexes: deleted.extend(indexes) or True)

    attempt = sm.send_sms_fallback(config, 'ALERT')
    assert attempt.success is True
    assert deleted == ['12']


def test_exit_code_precedence():
    assert sm.choose_exit_code(finding=False, operational_error=False, notification_ok=True) == 0
    assert sm.choose_exit_code(finding=True, operational_error=False, notification_ok=True) == 1
    assert sm.choose_exit_code(finding=True, operational_error=True, notification_ok=True) == 2
    assert sm.choose_exit_code(finding=True, operational_error=True, notification_ok=False) == 3


def test_smartctl_health_bits():
    critical, warning = sm.smartctl_health_bits((1 << 3) | (1 << 6) | (1 << 7))
    assert any('предстоящем отказе' in x for x in critical)
    assert any('Журнал ошибок SMART' in x for x in warning)
    assert not any('Журнал самотестов SMART' in x for x in warning)


def test_unexpected_start_phase_exception_cleans_up_owned_test(monkeypatch):
    d1 = sm.Disk('/dev/fake1', 'Disk 1', 'ata')
    d2 = sm.Disk('/dev/fake2', 'Disk 2', 'ata')
    before = {'ata_smart_self_test_log': {'standard': {'table': []}}}
    aborts = []

    def read_log(d, _general):
        if d.path == d2.path:
            raise RuntimeError('unexpected parser bug')
        return before

    def state(d, _general):
        return sm.SelfTestState(False, 'idle', None, {'ata_smart_data': {'self_test': {}}})

    def raw(d, _general, *args):
        if args == ('-X',):
            aborts.append(d.path)
        return cp(0)

    monkeypatch.setattr(sm, 'read_selftest_log', read_log)
    monkeypatch.setattr(sm, 'current_selftest_state', state)
    monkeypatch.setattr(sm, 'run_smartctl_raw', raw)
    monkeypatch.setattr(sm, 'selftest_timeout_minutes', lambda *a, **k: (100, 70))

    with pytest.raises(RuntimeError, match='unexpected parser bug'):
        sm.run_selftests([d1, d2], 'long', {})

    # State says the first test already ended, so cleanup must not send a stale -X.
    # The important invariant is that cleanup executes and verifies ownership state.
    assert aborts == []


def test_timeout_abort_failure_is_operational_failure(monkeypatch):
    d = disk()
    before = {'ata_smart_self_test_log': {'standard': {'table': []}}}
    calls = {'state': 0}

    monkeypatch.setattr(sm, 'read_selftest_log', lambda *a, **k: before)
    monkeypatch.setattr(sm, 'selftest_timeout_minutes', lambda *a, **k: (0, 169))
    monkeypatch.setattr(sm.time, 'sleep', lambda *_: None)

    def state(*args, **kwargs):
        calls['state'] += 1
        if calls['state'] == 1:
            return sm.SelfTestState(False, 'idle', None, {'ata_smart_data': {'self_test': {}}})
        return sm.SelfTestState(True, 'in progress', 10, {})

    def raw(_disk, _general, *args):
        if args == ('-X',):
            return cp(2, '', 'abort failed')
        return cp(0)

    monkeypatch.setattr(sm, 'current_selftest_state', state)
    monkeypatch.setattr(sm, 'run_smartctl_raw', raw)

    result = sm.run_selftests([d], 'long', {})[d.path]
    assert result.outcome == sm.SelfTestOutcome.TIMEOUT_ABORT_FAILED
    assert result.operational_failure is True
    assert result.abort_attempted is True
    assert result.abort_succeeded is False


def test_inactive_without_log_update_stops_after_settle_window(monkeypatch):
    d = disk()
    before = {'ata_smart_self_test_log': {'standard': {'table': []}}}
    clock = {'t': 0.0, 'state': 0}

    monkeypatch.setattr(sm, 'read_selftest_log', lambda *a, **k: before)
    monkeypatch.setattr(sm, 'selftest_timeout_minutes', lambda *a, **k: (100, 70))
    monkeypatch.setattr(sm, 'run_smartctl_raw', lambda *a, **k: cp(0))
    monkeypatch.setattr(sm.time, 'monotonic', lambda: clock['t'])

    def sleep(seconds):
        clock['t'] += seconds

    def state(*args, **kwargs):
        clock['state'] += 1
        if clock['state'] == 1:
            return sm.SelfTestState(False, 'idle', None, {'ata_smart_data': {'self_test': {}}})
        return sm.SelfTestState(False, 'completed', None, {'ata_smart_data': {'self_test': {}}})

    monkeypatch.setattr(sm.time, 'sleep', sleep)
    monkeypatch.setattr(sm, 'current_selftest_state', state)

    result = sm.run_selftests(
        [d],
        'short',
        {'poll_interval_seconds': 5, 'selftest_result_settle_seconds': 10},
    )[d.path]
    assert result.outcome == sm.SelfTestOutcome.RESULT_MISSING
    assert clock['t'] <= 15


def test_modem_lock_collision_prevents_api_use(monkeypatch, tmp_path):
    lock = tmp_path / 'modem.lock'
    config = {
        'sms_fallback': {
            'enabled': True,
            'modem_url': 'http://modem',
            'phone': '+70000000000',
            'lock_file': str(lock),
            'lock_wait_seconds': 1,
        }
    }
    holder = sm.acquire_lock(str(lock), wait_seconds=0)
    assert holder is not None
    try:
        monkeypatch.setattr(sm, 'list_sent_sms', lambda *a, **k: pytest.fail('modem API must not be called'))
        attempt = sm.send_sms_fallback(config, 'ALERT')
        assert attempt.success is False
        assert 'lock' in attempt.detail.lower() or 'блок' in attempt.detail.lower()
    finally:
        holder.close()

def test_device_type_is_passed_to_smartctl(monkeypatch):
    d = sm.Disk('/dev/fake', 'USB HDD', 'ata', 'sat')
    seen = {}
    monkeypatch.setattr(sm, 'resolve_disk_device', lambda _disk: '/dev/fake')
    monkeypatch.setattr(sm, 'smartctl_binary', lambda _general: '/usr/sbin/smartctl')

    def run(command, **kwargs):
        seen['command'] = command
        return cp(0, '{"smartctl":{"exit_status":0}}', '')

    monkeypatch.setattr(sm.subprocess, 'run', run)
    sm.run_smartctl_json(d, {}, '-a')
    assert seen['command'][:4] == ['/usr/sbin/smartctl', '-d', 'sat', '-j']


def test_secret_value_can_use_environment(monkeypatch):
    monkeypatch.setenv('SMART_MONITOR_TEST_TOKEN', ' secret-value ')
    assert sm.secret_value({'token_env': 'SMART_MONITOR_TEST_TOKEN'}, 'token') == 'secret-value'
    assert sm.secret_value({'token': 'literal'}, 'token') == 'literal'


def test_matrix_max_destination_selects_expected_channels():
    assert sm.selected_channels('matrix-max') == (False, True, True)


def test_sms_fallback_runs_when_single_internet_channel_fails(monkeypatch):
    monkeypatch.setattr(
        sm,
        'send_telegram',
        lambda *a, **k: sm.ChannelAttempt('telegram', True, False, 'fail'),
    )
    called = {'sms': 0}

    def sms(*a, **k):
        called['sms'] += 1
        return sm.ChannelAttempt('sms', True, True)

    monkeypatch.setattr(sm, 'send_sms_fallback', sms)
    assert sm.notify({}, 'x', 'telegram', sms_message='x') is True
    assert called['sms'] == 1


def test_configured_smartctl_path_wins(monkeypatch):
    monkeypatch.setattr(sm.shutil, 'which', lambda *_: '/some/other/smartctl')
    assert sm.smartctl_binary({'smartctl_path': '/custom/smartctl'}) == '/custom/smartctl'

def test_check_finding_triggers_notification(monkeypatch, tmp_path):
    fake_disk = sm.Disk('/dev/fake', 'Fake HDD', 'ata')
    status = sm.DiskStatus(disk=fake_disk, present=True, severity=2, status_text='Плохо')
    called = {'notify': 0}
    lock = tmp_path / 'main.lock'

    monkeypatch.setattr(
        sm,
        'parse_arguments',
        lambda: SimpleNamespace(
            mode='check',
            destination='none',
            no_sms_fallback=False,
            config=str(tmp_path / 'config.toml'),
            notify=False,
            debug=False,
        ),
    )
    monkeypatch.setattr(
        sm,
        'load_config',
        lambda _path: {
            'general': {'lock_file': str(lock)},
            'disks': [
                {'path': '/dev/fake', 'label': 'Fake HDD', 'kind': 'ata'}
            ],
        },
    )
    monkeypatch.setattr(sm, 'load_disks', lambda _cfg: [fake_disk])
    monkeypatch.setattr(sm, 'validate_general', lambda _cfg: {'lock_file': str(lock)})
    monkeypatch.setattr(sm, 'smartctl_binary', lambda _general: '/bin/true')
    monkeypatch.setattr(sm, 'read_disk_status', lambda *_: status)
    monkeypatch.setattr(sm, 'build_report', lambda *_: 'REPORT')
    monkeypatch.setattr(sm, 'build_sms_summary', lambda *_: 'SMS')

    def notify(*args, **kwargs):
        called['notify'] += 1
        return True

    monkeypatch.setattr(sm, 'notify', notify)
    assert sm.main() == sm.EXIT_FINDING
    assert called['notify'] == 1

def test_secret_value_falls_back_to_literal_when_environment_missing(monkeypatch):
    monkeypatch.delenv('SMART_MONITOR_MISSING_TOKEN', raising=False)
    section = {'token': 'literal', 'token_env': 'SMART_MONITOR_MISSING_TOKEN'}
    assert sm.secret_value(section, 'token') == 'literal'

def test_sms_fallback_does_not_run_when_external_channels_are_disabled(monkeypatch):
    monkeypatch.setattr(
        sm,
        'send_telegram',
        lambda *a, **k: sm.ChannelAttempt('telegram', False, None, 'disabled'),
    )
    monkeypatch.setattr(sm, 'send_matrix', lambda *a, **k: sm.ChannelAttempt('matrix', True, True))
    monkeypatch.setattr(sm, 'send_max', lambda *a, **k: sm.ChannelAttempt('max', False, None, 'disabled'))
    monkeypatch.setattr(sm, 'send_sms_fallback', lambda *a, **k: pytest.fail('fallback should not run'))
    assert sm.notify({}, 'x', 'all', sms_message='x') is True

def test_matrix_requires_event_id(monkeypatch):
    class Response:
        def __enter__(self):
            return self
        def __exit__(self, *args):
            return False
        def read(self, _limit=-1):
            return b'{}'

    class Opener:
        def open(self, *args, **kwargs):
            return Response()

    monkeypatch.setattr(sm, 'direct_opener', lambda: Opener())
    attempt = sm.send_matrix(
        {'matrix': {'enabled': True, 'url': 'https://matrix.example.org', 'room_id': '!x:y', 'token': 't'}},
        'hello',
    )
    assert attempt.success is False
    assert 'event_id' in attempt.detail

def test_main_lock_collision_returns_busy(monkeypatch, tmp_path):
    lock = tmp_path / 'main.lock'
    holder = sm.acquire_lock(str(lock), wait_seconds=0)
    assert holder is not None
    try:
        monkeypatch.setattr(
            sm,
            'parse_arguments',
            lambda: SimpleNamespace(
                mode='check',
                destination='none',
                no_sms_fallback=False,
                config=str(tmp_path / 'config.toml'),
                notify=False,
                debug=False,
            ),
        )
        monkeypatch.setattr(sm, 'load_config', lambda _path: {
            'general': {'lock_file': str(lock), 'lock_wait_seconds': 1},
            'disks': [{'path': '/dev/fake', 'label': 'Fake HDD', 'kind': 'ata'}],
        })
        monkeypatch.setattr(sm, 'load_disks', lambda _cfg: [disk()])
        monkeypatch.setattr(sm, 'validate_general', lambda cfg: cfg['general'])
        monkeypatch.setattr(sm, 'smartctl_binary', lambda _general: '/bin/true')
        assert sm.main() == sm.EXIT_BUSY
    finally:
        holder.close()

def test_telegram_proxy_password_can_use_environment(monkeypatch):
    monkeypatch.setenv('SMART_MONITOR_PROXY_PASSWORD', 'p@ss')
    captured = {}

    def auth(url, username, password):
        captured['password'] = password
        return url

    monkeypatch.setattr(sm, 'proxy_url_with_auth', auth)
    sm.telegram_opener({
        'proxy_enabled': True,
        'proxy_url': 'http://127.0.0.1:3128',
        'proxy_username': 'user',
        'proxy_password_env': 'SMART_MONITOR_PROXY_PASSWORD',
    })
    assert captured['password'] == 'p@ss'

def test_ata_temperature_threshold_sets_warning(monkeypatch):
    d = sm.Disk(
        '/dev/fake',
        'Hot HDD',
        'ata',
        temperature_warning=45,
        temperature_critical=60,
    )
    payload = {
        'smartctl': {'exit_status': 0},
        'smart_status': {'passed': True},
        'temperature': {'current': 50},
        'ata_smart_attributes': {'table': []},
    }
    monkeypatch.setattr(sm, 'run_smartctl_json', lambda *a, **k: (payload, 0))
    status = sm.read_disk_status(d, {})
    assert status.severity == 1
    assert any('Температура 50°C' in issue for issue in status.issues)


def test_ata_temperature_threshold_sets_critical(monkeypatch):
    d = sm.Disk(
        '/dev/fake',
        'Hot HDD',
        'ata',
        temperature_warning=45,
        temperature_critical=60,
    )
    payload = {
        'smartctl': {'exit_status': 0},
        'smart_status': {'passed': True},
        'temperature': {'current': 61},
        'ata_smart_attributes': {'table': []},
    }
    monkeypatch.setattr(sm, 'run_smartctl_json', lambda *a, **k: (payload, 0))
    status = sm.read_disk_status(d, {})
    assert status.severity == 2
    assert any('критического порога' in issue for issue in status.issues)


def test_nvme_wear_threshold_sets_warning(monkeypatch):
    d = sm.Disk(
        '/dev/fake',
        'NVMe',
        'nvme',
        wear_warning_percent=90,
        wear_critical_percent=100,
    )
    payload = {
        'smartctl': {'exit_status': 0},
        'smart_status': {'passed': True},
        'temperature': {'current': 40},
        'nvme_smart_health_information_log': {
            'critical_warning': 0,
            'percentage_used': 95,
            'available_spare': 100,
            'media_errors': 0,
            'num_err_log_entries': 0,
        },
    }
    monkeypatch.setattr(sm, 'run_smartctl_json', lambda *a, **k: (payload, 0))
    status = sm.read_disk_status(d, {})
    assert status.severity == 1
    assert any('Износ NVMe 95%' in issue for issue in status.issues)


def test_disk_threshold_validation_rejects_bad_order():
    config = {
        'disks': [{
            'path': '/dev/fake',
            'label': 'Fake',
            'kind': 'ata',
            'temperature_warning': 60,
            'temperature_critical': 50,
        }]
    }
    with pytest.raises(sm.MonitorError, match='temperature_critical'):
        sm.load_disks(config)


def test_selftest_signature_uses_entire_table_for_same_hour_results():
    d = disk()
    newest = {
        'num': 1,
        'type': {'string': 'Short offline'},
        'status': {'string': 'Completed without error', 'passed': True},
        'lifetime_hours': 100,
    }
    older = {
        'num': 2,
        'type': {'string': 'Short offline'},
        'status': {'string': 'Completed without error', 'passed': True},
        'lifetime_hours': 99,
    }
    before = {
        'ata_smart_self_test_log': {
            'standard': {'table': [newest, older]}
        }
    }
    shifted_newest = dict(newest)
    shifted_newest['num'] = 2
    after = {
        'ata_smart_self_test_log': {
            'standard': {'table': [newest, shifted_newest, older]}
        }
    }

    assert before['ata_smart_self_test_log']['standard']['table'][0] == after['ata_smart_self_test_log']['standard']['table'][0]
    assert sm.selftest_log_signature(d, before) != sm.selftest_log_signature(d, after)


def test_auto_kind_detects_nvme(monkeypatch):
    d = sm.Disk('/dev/fake', 'Auto disk', 'auto')
    payload = {
        'smartctl': {'exit_status': 0},
        'device': {'type': 'nvme', 'protocol': 'NVMe'},
    }
    monkeypatch.setattr(sm, 'run_smartctl_json', lambda *a, **k: (payload, 0))
    detected = sm.detect_disk_kind(d, {})
    assert detected.kind == 'nvme'
    assert detected.path == d.path
    assert detected.label == d.label


def test_auto_kind_detects_ata(monkeypatch):
    d = sm.Disk('/dev/fake', 'Auto disk', 'auto', 'sat')
    payload = {
        'smartctl': {'exit_status': 0},
        'device': {'type': 'sat', 'protocol': 'ATA'},
    }
    monkeypatch.setattr(sm, 'run_smartctl_json', lambda *a, **k: (payload, 0))
    detected = sm.detect_disk_kind(d, {})
    assert detected.kind == 'ata'
    assert detected.device_type == 'sat'


def test_auto_kind_rejects_nvme_wear_thresholds_on_detected_ata(monkeypatch):
    d = sm.Disk(
        '/dev/fake',
        'Auto disk',
        'auto',
        wear_warning_percent=80,
        wear_critical_percent=95,
    )
    payload = {
        'smartctl': {'exit_status': 0},
        'device': {'type': 'sat', 'protocol': 'ATA'},
    }
    monkeypatch.setattr(sm, 'run_smartctl_json', lambda *a, **k: (payload, 0))
    with pytest.raises(sm.MonitorError, match='пороги износа'):
        sm.detect_disk_kind(d, {})


def test_sms_summary_uses_configured_temperature_and_wear_thresholds():
    d = sm.Disk(
        '/dev/fake',
        'Warm NVMe',
        'nvme',
        temperature_warning=45,
        temperature_critical=60,
        wear_warning_percent=80,
        wear_critical_percent=95,
    )
    status = sm.DiskStatus(
        disk=d,
        present=True,
        severity=1,
        status_text='Тревога',
        temperature=50,
        metrics={
            'critical_warning': 0,
            'percentage_used': 85,
            'available_spare': 100,
            'media_errors': 0,
            'error_entries': 0,
        },
    )
    text = sm.build_sms_summary('host', [status])
    assert 'температура 50C' in text
    assert 'износ 85%' in text


def test_auto_kind_detection_failure_keeps_disk_for_health_report(monkeypatch):
    d = sm.Disk('/dev/missing', 'Missing disk', 'auto')

    def fail(*args, **kwargs):
        raise sm.MonitorError('device missing')

    monkeypatch.setattr(sm, 'run_smartctl_json', fail)
    detected = sm.detect_disk_kind(d, {})
    assert detected.kind == 'auto'

    status = sm.read_disk_status(detected, {})
    assert status.operational_error is True
    assert status.severity == 2
    assert any('device missing' in issue for issue in status.issues)


def test_unresolved_auto_kind_selftest_is_not_started(monkeypatch):
    d = sm.Disk('/dev/missing', 'Missing disk', 'auto')
    monkeypatch.setattr(
        sm,
        'run_smartctl_raw',
        lambda *a, **k: pytest.fail('smartctl -t must not be called'),
    )
    result = sm.run_selftests([d], 'short', {})[d.path]
    assert result.outcome == sm.SelfTestOutcome.START_FAILED
    assert 'тип накопителя не определён' in result.detail


def test_split_message_preserves_text_and_limits_chunks():
    message = ("строка 1\n" * 1000) + ("X" * 5000) + "\nконец"
    chunks = sm.split_message(message, 4000)
    assert "".join(chunks) == message
    assert len(chunks) > 1
    assert all(0 < len(chunk) <= 4000 for chunk in chunks)


def test_telegram_splits_long_report(monkeypatch):
    sent: list[str] = []

    class Response:
        def __enter__(self):
            return self
        def __exit__(self, *args):
            return False
        def read(self, _limit=-1):
            return b'{"ok":true,"result":{"message_id":1}}'

    class Opener:
        def open(self, request, **kwargs):
            data = urllib.parse.parse_qs(request.data.decode('utf-8'))
            sent.append(data['text'][0])
            return Response()

    import urllib.parse
    monkeypatch.setattr(sm, 'telegram_opener', lambda _cfg: Opener())
    message = 'A' * 8500
    attempt = sm.send_telegram(
        {'telegram': {'enabled': True, 'token': 't', 'chat_id': '1'}},
        message,
    )
    assert attempt.success is True
    assert ''.join(sent) == message
    assert len(sent) == 3
    assert all(len(part) <= 4000 for part in sent)


def test_max_splits_long_report_and_confirms_each_part(monkeypatch):
    sent: list[str] = []
    sleeps: list[float] = []

    class Response:
        def __enter__(self):
            return self
        def __exit__(self, *args):
            return False
        def read(self, _limit=-1):
            return b'{"message":{"body":{"text":"ok"}}}'

    class Opener:
        def open(self, request, **kwargs):
            payload = json.loads(request.data.decode('utf-8'))
            sent.append(payload['text'])
            return Response()

    import json
    monkeypatch.setattr(sm, 'direct_opener', lambda: Opener())
    monkeypatch.setattr(sm.time, 'sleep', lambda value: sleeps.append(value))
    message = 'B' * 8000
    attempt = sm.send_max(
        {'max': {'enabled': True, 'bot_token': 't', 'chat_id': '1'}},
        message,
    )
    assert attempt.success is True
    assert ''.join(sent) == message
    assert len(sent) == 3
    assert all(len(part) <= 3900 for part in sent)
    assert sleeps == [0.55, 0.55]


def test_max_requires_message_confirmation(monkeypatch):
    class Response:
        def __enter__(self):
            return self
        def __exit__(self, *args):
            return False
        def read(self, _limit=-1):
            return b'{}'

    class Opener:
        def open(self, *args, **kwargs):
            return Response()

    monkeypatch.setattr(sm, 'direct_opener', lambda: Opener())
    attempt = sm.send_max(
        {'max': {'enabled': True, 'bot_token': 't', 'chat_id': '1'}},
        'hello',
    )
    assert attempt.success is False
    assert 'не подтвердил' in attempt.detail


def test_old_ata_selftest_failure_does_not_warn_when_latest_passed():
    d = disk()
    status = sm.DiskStatus(disk=d, present=True, smartctl_exit_status=sm.SMARTCTL_SELFTEST_LOG)
    data = {
        'smartctl': {'exit_status': sm.SMARTCTL_SELFTEST_LOG},
        'smart_status': {'passed': True},
        'temperature': {'current': 30},
        'ata_smart_attributes': {'table': []},
        'ata_smart_self_test_log': {
            'standard': {
                'table': [
                    {'status': {'passed': True, 'string': 'Completed without error'}},
                    {'status': {'passed': False, 'string': 'Aborted by host'}},
                ]
            }
        },
    }
    sm.read_ata_status(data, status)
    assert status.severity == 0
    assert status.issues == []


def test_latest_ata_selftest_failure_is_reported():
    d = disk()
    status = sm.DiskStatus(disk=d, present=True, smartctl_exit_status=sm.SMARTCTL_SELFTEST_LOG)
    data = {
        'smartctl': {'exit_status': sm.SMARTCTL_SELFTEST_LOG},
        'smart_status': {'passed': True},
        'temperature': {'current': 30},
        'ata_smart_attributes': {'table': []},
        'ata_smart_self_test_log': {
            'standard': {
                'table': [
                    {'status': {'passed': False, 'string': 'Completed: read failure'}},
                ]
            }
        },
    }
    sm.read_ata_status(data, status)
    assert status.severity == 2
    assert any('Последний самотест ATA' in issue for issue in status.issues)


def test_latest_ata_host_abort_is_warning_not_disk_failure():
    d = disk()
    status = sm.DiskStatus(disk=d, present=True, smartctl_exit_status=sm.SMARTCTL_SELFTEST_LOG)
    data = {
        'smartctl': {'exit_status': sm.SMARTCTL_SELFTEST_LOG},
        'smart_status': {'passed': True},
        'temperature': {'current': 30},
        'ata_smart_attributes': {'table': []},
        'ata_smart_self_test_log': {
            'standard': {
                'table': [
                    {'status': {'passed': False, 'string': 'Aborted by host'}},
                ]
            }
        },
    }
    sm.read_ata_status(data, status)
    assert status.severity == 1
    assert any('был прерван' in issue for issue in status.issues)


def test_latest_nvme_selftest_failure_is_warning():
    d = sm.Disk('/dev/fake', 'NVMe', 'nvme')
    status = sm.DiskStatus(disk=d, present=True)
    data = {
        'nvme_self_test_log': {
            'table': [
                {
                    'self_test_result': {
                        'value': 5,
                        'string': 'Completed with a segment failure',
                    }
                }
            ]
        }
    }
    critical: list[str] = []
    warning: list[str] = []
    sm.apply_latest_selftest_health(status, data, critical, warning)
    assert critical == []
    assert any('Последний самотест NVMe' in issue for issue in warning)
