#!/bin/sh
set -eu

# Keep Chromium itself out of the image layers (system libraries are baked in by the Dockerfile).
# Playwright caches the browser under PLAYWRIGHT_BROWSERS_PATH, so only the first start downloads it.
playwright install --no-shell chromium
exec python app.py
