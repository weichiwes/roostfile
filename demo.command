#!/bin/sh
# Double-click on a Mac to try the app on sample files.
cd "$(dirname "$0")" || exit 1
exec python3 desktop_app.py --demo
