#!/bin/sh
# Double-click on a Mac. Needs Python 3.12 or later; see README.md.
cd "$(dirname "$0")" || exit 1
exec python3 desktop_app.py "$@"
