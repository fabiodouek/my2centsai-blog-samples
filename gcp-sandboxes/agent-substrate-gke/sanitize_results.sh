#!/usr/bin/env bash
# Replace the project ID, project number and bucket name in results/ before committing them.
cd "$(dirname "$0")"; source ./env.sh
for f in results/*; do
  perl -pi -e "s/\Q$BUCKET_NAME\E/<bucket>/g; s/\Q$PROJECT_ID\E/<project>/g; s/\Q$PROJECT_NUMBER\E/<project-number>/g" "$f"
done
grep -rIl "$PROJECT_ID\|$PROJECT_NUMBER" results || echo "results clean"
