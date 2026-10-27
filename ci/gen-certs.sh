#!/usr/bin/env bash
# Throwaway CA + leaf certificate for Mailpit (the fake upstream relay in CI).
set -euo pipefail
dir="$(cd "$(dirname "$0")" && pwd)/certs"
mkdir -p "$dir"
cd "$dir"
openssl req -x509 -newkey ec -pkeyopt ec_paramgen_curve:prime256v1 -nodes -days 2 \
  -subj "/CN=JuMail CI CA" -keyout ca.key -out ca.pem >/dev/null 2>&1
openssl req -newkey ec -pkeyopt ec_paramgen_curve:prime256v1 -nodes \
  -subj "/CN=mailpit" -addext "subjectAltName=DNS:mailpit" \
  -keyout mailpit.key -out mailpit.csr >/dev/null 2>&1
openssl x509 -req -in mailpit.csr -CA ca.pem -CAkey ca.key -CAcreateserial -days 2 \
  -copy_extensions copy -out mailpit.pem >/dev/null 2>&1
rm -f mailpit.csr ca.srl
chmod 644 ca.pem mailpit.pem mailpit.key
echo "CI certificates written to $dir"
