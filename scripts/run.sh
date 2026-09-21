#!/bin/sh
# Locate a usable Python 3.9+ and hand off to src/pane_pwd.py.
#
# Herdr launches plugin commands from the server process, whose PATH can be
# much smaller than an interactive shell's, so the common absolute locations
# are probed explicitly rather than trusting `python3` to resolve.

set -eu

root=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)

for py in \
    "${HERDR_PANE_PWD_PYTHON:-}" \
    python3 \
    /opt/homebrew/bin/python3 \
    /usr/local/bin/python3 \
    /usr/bin/python3
do
    [ -n "$py" ] || continue
    command -v "$py" >/dev/null 2>&1 || continue
    if "$py" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 9) else 1)' >/dev/null 2>&1; then
        exec "$py" "$root/src/pane_pwd.py" "$@"
    fi
done

printf 'pane-pwd: no Python 3.9+ interpreter found on PATH\n' >&2
exit 127
