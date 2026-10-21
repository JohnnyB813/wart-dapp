"""Check the packaged GUI starts with an in-memory wallet and no node access."""
import os
from pathlib import Path
import subprocess
import sys
import tempfile


def main():
    executable = str(Path(sys.argv[1]).resolve(strict=True))
    with tempfile.TemporaryDirectory(prefix='wart-smoke-') as directory:
        report = Path(directory) / 'startup-error.txt'
        env = dict(os.environ, QT_QPA_PLATFORM='offscreen', WART_SMOKE_REPORT=str(report))
        process = subprocess.Popen([executable, '--wallet-db', ':memory:', '--smoke-test'], env=env)
        try:
            result = process.wait(timeout=40)
        except subprocess.TimeoutExpired:
            if os.name == 'nt':
                subprocess.run(['taskkill', '/PID', str(process.pid), '/T', '/F'], check=False)
            else:
                process.kill()
            process.wait()
            raise SystemExit('Packaged wallet did not finish its startup check.')
        if result:
            if report.exists():
                print(report.read_text(encoding='utf-8'))
            raise SystemExit(f'Packaged wallet exited with code {result}.')
    print('Packaged wallet startup: PASS (in-memory database, no node access).')


if __name__ == '__main__':
    main()
