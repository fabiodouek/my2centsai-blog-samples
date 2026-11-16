#!/usr/bin/env bash
# Filter for recorded runs: shortens gcloud's wall of progress dots to "..." and replaces the project ID, project number, bucket name and control-plane IP with placeholders.
sed -u -e 's/\.\{20,\}/.../g' -e "s/${PROJECT:-__none__}/<project>/g" -e "s/${PROJECT_NUMBER:-__none__}/<project-number>/g" -e "s/${BUCKET:-__none__}/<bucket>/g" \
  -e 's/\([0-9]\{1,3\}\.\)\{3\}[0-9]\{1,3\}\( *\)RUNNING/<ip>\2RUNNING/'
