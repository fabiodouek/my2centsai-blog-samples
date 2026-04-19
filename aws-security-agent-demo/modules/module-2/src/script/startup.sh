#!/bin/bash
set -x

DB_USER="root"
DB_PASS="T2kVB3zgeN3YbrKS"

for i in $(seq 1 30); do
  if mysql -h "$RDS_ENDPOINT" -P 3306 -u "$DB_USER" -p"$DB_PASS" -e "SELECT 1" >/dev/null 2>&1; then
    echo "RDS reachable after ${i} attempts"
    break
  fi
  echo "Waiting for RDS (attempt ${i})..."
  sleep 2
done

if ! mysql -h "$RDS_ENDPOINT" -P 3306 -u "$DB_USER" -p"$DB_PASS" -e "USE appdb" >/dev/null 2>&1; then
  echo "appdb missing — loading /var/www/html/dump.sql"
  mysql -h "$RDS_ENDPOINT" -P 3306 -u "$DB_USER" -p"$DB_PASS" < /var/www/html/dump.sql
  if [ $? -ne 0 ]; then
    echo "ERROR: dump.sql load failed" >&2
  fi
else
  echo "appdb already present — skipping dump"
fi

sed -i "s,RDS_ENDPOINT_VALUE,$RDS_ENDPOINT,g" /var/www/html/config.inc
exec apache2-foreground
