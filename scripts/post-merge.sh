#!/bin/bash
# Replit postMerge hook. The Python backend owns DATABASE_URL and migrates itself at
# startup (backend/app/db.py migrate_engine); never run a JS schema push against it.
set -e
pnpm install --frozen-lockfile
