"""Verify the archive from an isolated extraction without using model calls."""
from __future__ import annotations

import hashlib
import json
import re
import subprocess
import sys
import zipfile
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def main():
    archive_path = ROOT / 'code.zip'
    workspace = ROOT / 'code/runs' / ('package-check-' + datetime.now(timezone.utc).strftime('%Y%m%d%H%M%S'))
    destination = workspace / 'code'
    destination.mkdir(parents=True, exist_ok=False)
    with zipfile.ZipFile(archive_path) as archive:
        names = set(archive.namelist())
        required = {'main.py', 'README.md', 'requirements.txt', 'pytest.ini', '.env.example',
                    'evaluation/usage_report.md', 'evaluation/sample_report.json',
                    'prompts/system.txt', 'tests/fixtures/media/images/image_attack.png'}
        assert required <= names, f'Missing package files: {required - names}'
        for name in names:
            path = Path(name)
            assert not path.is_absolute() and '..' not in path.parts
            assert not any(p in {'.env', '.venv', 'runs', '__pycache__'} for p in path.parts)
            assert (destination / path).resolve().is_relative_to(destination.resolve())
            data = archive.read(name)
            assert not re.search(rb'AIza[\w-]{25,}|-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----', data), 'Credential pattern in archive'
        archive.extractall(destination)
    commands = [
        [sys.executable, str(destination/'main.py'), '--help'],
        [sys.executable, str(destination/'main.py'), 'check', '--dataset', str(ROOT/'dataset')],
        [sys.executable, '-m', 'pytest', '-c', str(destination/'pytest.ini'), str(destination/'tests'), '-q'],
    ]
    results=[]
    for command in commands:
        result=subprocess.run(command,cwd=workspace,capture_output=True,text=True,timeout=60)
        results.append({'command':command[1:],'exit_code':result.returncode,'stdout':result.stdout,'stderr':result.stderr})
        if result.returncode:
            raise RuntimeError(result.stdout + result.stderr)
    report={'checked_at':datetime.now(timezone.utc).isoformat(),'archive_sha256':hashlib.sha256(archive_path.read_bytes()).hexdigest(),
            'file_count':len(names),'credential_pattern_check':'passed','checks':results,
            'limitation':'Uses the installed pinned dependencies; this is an isolated source extraction, not a fresh dependency installation.'}
    (ROOT/'code/evaluation/package_check.json').write_text(json.dumps(report,indent=2)+'\n',encoding='utf-8')
    print(f'Archive verified: {len(names)} files; entry point, input validation and extracted tests pass.')


if __name__=='__main__':
    main()
