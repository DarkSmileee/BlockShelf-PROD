"""Run only the backup dispatcher with fake backup functions; no files are backed up."""
from pathlib import Path
import subprocess
import pytest
from tests.test_update_safety import BASH, ROOT


@pytest.mark.parametrize('fail_sqlite,expected', [(False, 0), (True, 1)])
def test_both_backup_branches_and_cleanup_run(fail_sqlite, expected):
    if not BASH.exists():
        pytest.skip('Bash unavailable')
    probe = subprocess.run([str(BASH), '-c', 'exit 0'], capture_output=True)
    if probe.returncode:
        pytest.skip('Bash blocked by sandbox')
    source = (ROOT/'scripts/backup_database.sh').read_text()
    main = source[source.index('main() {'):source.index('# Run main function')]
    setup = """set -euo pipefail
TIMESTAMP=test
log_info() { :; }
log_warn() { :; }
log_success() { :; }
log_error() { :; }
backup_sqlite() { echo sqlite; return SQLITE_STATUS; }
backup_postgres() { echo postgres; return 0; }
cleanup_old_backups() { echo cleanup; }
""".replace('SQLITE_STATUS', '1' if fail_sqlite else '0')
    result = subprocess.run([str(BASH), '-c', setup + main + '\nmain both\n'], capture_output=True, text=True)
    assert result.returncode == expected, result.stderr
    assert result.stdout.splitlines() == ['sqlite', 'postgres', 'cleanup']


def test_deployment_shell_syntax():
    if not BASH.exists():
        pytest.skip('Bash unavailable')
    probe = subprocess.run([str(BASH), '-c', 'exit 0'], capture_output=True)
    if probe.returncode:
        pytest.skip('Bash blocked by sandbox')
    for script in (ROOT/'scripts').glob('*.sh'):
        result = subprocess.run([str(BASH), '-n', str(script)], capture_output=True, text=True)
        assert result.returncode == 0, f'{script}: {result.stderr}'
