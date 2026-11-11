#!/bin/bash
# Restore to a NEW destination only. The live database is never dropped.
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PYTHON="${BLOCKSHELF_PYTHON:-$SCRIPT_DIR/../.venv/bin/python}"
exec "$PYTHON" "$SCRIPT_DIR/restore_database.py" "$@"
