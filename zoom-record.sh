#!/bin/bash
# Thin wrapper. The real implementation lives in zoom_record.py.
# Kept so existing invocations like `./zoom-record.sh 10` keep working.
exec "$(dirname "$0")/zoom_record.py" "$@"
