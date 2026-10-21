"""Record the exact source inputs and revision used by a local or CI package."""
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import platform
import subprocess

root = Path(__file__).resolve().parents[1]
version = (root / 'version.txt').read_text().strip()


def git(*arguments):
    return subprocess.check_output(['git', *arguments], cwd=root, text=True).strip()


digest = hashlib.sha256()
inputs = sorted([*root.glob('*.py'), *root.glob('requirements*.txt'), root / 'version.txt'], key=lambda p: p.name)
for path in inputs:
    digest.update(path.name.encode() + b'\0' + path.read_bytes() + b'\0')
metadata = dict(version=version, revision=git('rev-parse', 'HEAD'), dirty=bool(git('status', '--porcelain')),
                built_at=datetime.now(timezone.utc).isoformat(timespec='seconds'),
                python=platform.python_version(), architecture=platform.machine(),
                source_sha256=digest.hexdigest())
output = root / 'build' / 'generated' / 'build-info.json'
output.parent.mkdir(parents=True, exist_ok=True)
output.write_text(json.dumps(metadata, indent=2) + '\n', encoding='utf-8')
print(f"Build metadata: {version}, {metadata['revision'][:12]}, dirty={metadata['dirty']}")
