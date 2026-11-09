import json
import zipfile
from datetime import datetime
from pathlib import Path

root = Path(__file__).resolve().parent.parent
archive = root / 'backups' / f"QQBot-{datetime.now():%Y%m%d-%H%M%S}.zip"
partial = archive.with_suffix('.partial')
with zipfile.ZipFile(partial, 'w', zipfile.ZIP_DEFLATED) as target:
    for folder in ['data', 'config', 'scripts']:
        for file in (root / folder).rglob('*'):
            relative = file.relative_to(root)
            if any(part in {'logs', 'temp', '__pycache__', 'qq-profile'} for part in relative.parts):
                continue
            if file.is_file():
                target.write(file, relative.as_posix())
    target.write(root / 'deployment.json', 'deployment.json')
    for file in [*root.glob('*.cmd'), root / '使用说明.txt', root / '翻唱使用说明.txt']:
        if file.is_file():
            target.write(file, file.name)
with zipfile.ZipFile(partial) as target:
    if target.testzip():
        raise RuntimeError('Backup integrity verification failed')
partial.rename(archive)
print(f'Backup saved and verified: {archive}')
