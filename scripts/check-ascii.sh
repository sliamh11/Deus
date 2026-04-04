#!/usr/bin/env bash
# Pre-commit check: reject non-ASCII characters in shell/PowerShell scripts.
# Windows PowerShell 5.1 reads .ps1 files as ANSI (no UTF-8 BOM), so multi-byte
# Unicode chars (em-dashes, smart quotes, etc.) corrupt the parser.

status=0
for file in "$@"; do
  if LC_ALL=C grep -n '[^ -~	]' "$file"; then
    echo "error: non-ASCII characters found in $file (see above)"
    status=1
  fi
done
exit $status
