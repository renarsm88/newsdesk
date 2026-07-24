#!/bin/zsh
# Double-click me to fetch fresh headlines + battery, then open the page.
cd "$(dirname "$0")"
python3 build.py && open index.html
