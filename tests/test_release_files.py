from __future__ import annotations

import re
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_release_version_is_consistent():
    version = (ROOT / 'VERSION').read_text(encoding='utf-8').strip()
    pyproject = tomllib.loads((ROOT / 'pyproject.toml').read_text(encoding='utf-8'))
    source = (ROOT / 'smart_monitor.py').read_text(encoding='utf-8')

    assert version == '1.0.0'
    assert pyproject['project']['version'] == version
    assert f'VERSION = "{version}"' in source


def test_example_config_is_valid_toml():
    with (ROOT / 'config.example.toml').open('rb') as fh:
        config = tomllib.load(fh)

    assert config['general']['default_destination'] == 'none'
    assert config['disks']


def test_systemd_service_serializes_persistent_jobs():
    unit = (
        ROOT / 'deploy-examples/systemd/smart-monitor@.service'
    ).read_text(encoding='utf-8')

    assert '--wait-lock' in unit
    assert 'TimeoutStartSec=infinity' in unit
    assert 'SuccessExitStatus=1 4' in unit


def test_markdown_relative_links_exist():
    missing: list[str] = []

    for md in ROOT.glob('*.md'):
        text = md.read_text(encoding='utf-8')
        for target in re.findall(r'\[[^\]]+\]\(([^)]+)\)', text):
            if '://' in target or target.startswith('#') or target.startswith('mailto:'):
                continue
            relative = target.split('#', 1)[0]
            if relative and not (md.parent / relative).exists():
                missing.append(f'{md.name}: {target}')

    assert missing == []


def test_license_contains_standard_mit_disclaimer():
    text = (ROOT / 'LICENSE').read_text(encoding='utf-8')
    assert text.startswith('MIT License\n')
    assert 'THE SOFTWARE IS PROVIDED "AS IS"' in text
    normalized = ' '.join(text.split())
    assert 'IN NO EVENT SHALL THE AUTHORS OR COPYRIGHT HOLDERS BE LIABLE' in normalized


def test_public_tree_has_no_known_private_runtime_markers():
    forbidden = (
        'evs' + '-server',
        '/srv/' + 'services/smart-monitor',
        '/srv/' + 'github/smart-monitor',
        'matrix.' + 'evs.msk.ru',
        '192.168.' + '100.3',
    )
    checked_suffixes = {'.py', '.md', '.toml', '.service', '.timer', '.yml', '.example'}

    hits: list[str] = []
    for path in ROOT.rglob('*'):
        if not path.is_file():
            continue
        if any(part in {'.git', '.pytest_cache', '__pycache__'} for part in path.parts):
            continue
        if path.suffix not in checked_suffixes and path.name not in {'VERSION', 'LICENSE', 'install.sh', '.gitignore'}:
            continue
        text = path.read_text(encoding='utf-8', errors='replace')
        for marker in forbidden:
            if marker in text:
                hits.append(f'{path.relative_to(ROOT)}: {marker}')

    assert hits == []
