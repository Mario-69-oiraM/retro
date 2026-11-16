#!/bin/bash
# Double-click to start the retro board. Keep this window open during the retro.
cd "$(dirname "$0")"
python3 retro.py "$@"
echo; read -n 1 -s -r -p "Press any key to close..."
