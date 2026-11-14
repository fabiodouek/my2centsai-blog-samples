#!/usr/bin/env bash
# Filter for recorded runs: replaces the project ID, project number and snapshot bucket with placeholders and
# shortens long progress-dot lines. Usage: some-command 2>&1 | ./redact.sh
sed -u -e 's/\.\{20,\}/.../g' -e "s/${PROJECT_ID:-__none__}/<project>/g" -e "s/${PROJECT_NUMBER:-__none__}/<project-number>/g" \
  -e "s/${BUCKET_NAME:-__none__}/<bucket>/g" -e 's#/Users/[A-Za-z0-9._-]*#~#g'
