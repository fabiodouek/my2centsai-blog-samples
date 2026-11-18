#!/usr/bin/env bash
# Installs the az aigateway extension from the Azure/aigateway release installer.
set -euo pipefail
az version --query '"azure-cli"' -o tsv
curl -fsSL https://aka.ms/aigateway-cli-install -o /tmp/aigateway-install.sh
sh /tmp/aigateway-install.sh
az aigateway version
