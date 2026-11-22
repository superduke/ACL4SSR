#!/usr/bin/env bash
# Commit only the updater's outputs; an unchanged list is a successful run.
set -euo pipefail
message=$1
shift
if (( $# == 0 )); then
  echo 'At least one output path is required' >&2
  exit 1
fi
git config --local user.email "action@github.com"
git config --local user.name "GitHub Action"
git add -- "$@"
if git diff --cached --quiet -- "$@"; then
  echo 'No list changes; skipping commit and push'
  exit 0
fi
git commit -m "$message" -- "$@"
git push
