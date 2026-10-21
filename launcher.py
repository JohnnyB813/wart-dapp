"""Executable entry point, including non-interactive packaging diagnostics."""
import os
import sys
import traceback


def run():
    try:
        from main import run as run_wallet
        return run_wallet()
    except Exception:
        # A windowed PyInstaller crash dialog would hang an automated smoke check.
        # Only an explicitly requested smoke test writes this diagnostic file.
        report = os.environ.get('WART_SMOKE_REPORT')
        if '--smoke-test' in sys.argv and report:
            with open(report, 'w', encoding='utf-8') as stream:
                traceback.print_exc(file=stream)
            return 1
        raise


if __name__ == '__main__':
    sys.exit(run())
