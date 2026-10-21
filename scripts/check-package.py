"""Verify release checksums and that the ZIP contains no wallet databases."""
import hashlib
from pathlib import Path
import sys
import zipfile
from PyInstaller.archive.readers import CArchiveReader
import pefile

folder = Path(sys.argv[1]).resolve(strict=True)
for line in (folder / 'SHA256SUMS.txt').read_text().splitlines():
    expected, name = line.split('  ', 1)
    path = folder / name
    if path.parent != folder or hashlib.sha256(path.read_bytes()).hexdigest() != expected:
        raise SystemExit(f'Checksum mismatch: {name}')
archives = list(folder.glob('wart-dapp-*-windows-x64.zip'))
if len(archives) != 1:
    raise SystemExit('Expected exactly one release ZIP.')
version = (folder / 'version.txt').read_text().strip()
executable = folder / f'wart-dapp-{version}-windows-x64.exe'
bundle = CArchiveReader(str(executable))
for name in ('version.txt', 'build-info.json'):
    if bundle.extract(name) != (folder / name).read_bytes():
        raise SystemExit(f'Bundled metadata mismatch: {name}')
pe = pefile.PE(str(executable))
details = pe.VS_FIXEDFILEINFO[0]
actual_version = (details.FileVersionMS >> 16, details.FileVersionMS & 65535,
                  details.FileVersionLS >> 16, details.FileVersionLS & 65535)
expected_version = tuple(map(int, version.split('-')[0].split('.'))) + (0,)
if actual_version != expected_version:
    raise SystemExit('Windows executable version does not match the release.')
with zipfile.ZipFile(archives[0]) as archive:
    if archive.testzip() is not None:
        raise SystemExit('Release ZIP integrity check failed.')
    names = archive.namelist()
    if any(Path(name).suffix.lower() in ('.db', '.db3', '.sqlite', '.wartbackup') for name in names):
        raise SystemExit('A wallet database or backup was included in the release.')
    for name in names:
        if archive.read(name) != (folder / name).read_bytes() and name != 'SHA256SUMS.txt':
            raise SystemExit(f'ZIP differs from release files: {name}')
    if archive.read('version.txt').strip() != (folder / 'version.txt').read_bytes().strip():
        raise SystemExit('ZIP version mismatch.')
print('Release checksums and ZIP contents: PASS')
