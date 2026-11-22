#!/usr/bin/env bash
# Run on vm-emr (as root, through an Azure run command) once OpenEMR is up:
#   ORACLE_B64=<base64 of oracle_service.py> bash emr_services.sh
# 1. Records OpenEMR's product-registration opt-out, so no registration dialog
#    covers the menu after sign-in (nothing is sent anywhere).
# 2. Starts the lab's record service (oracle_service.py, "emr" mode) on the
#    private address; it reads patient_data through docker exec.
set -euo pipefail
: "${ORACLE_B64:?}"
for _ in $(seq 1 60); do
  curl -fs -o /dev/null http://127.0.0.1:8390/interface/login/login.php && break
  sleep 10
done
db=(docker exec -i openemr-live-mysql-1 mariadb -uroot -proot openemr)
ver=$("${db[@]}" -N -e "SELECT CONCAT(v_major,'.',v_minor,'.',v_patch,' (',v_realpatch,')') FROM version" 2>/dev/null || true)
echo "openemr version string: ${ver:-unknown}"
"${db[@]}" -e "DELETE FROM product_registration; INSERT INTO product_registration (opt_out, telemetry_disabled, last_ask_date, last_ask_version) VALUES (1, 1, NOW(), '${ver}');" \
  || "${db[@]}" -e "DELETE FROM product_registration; INSERT INTO product_registration (opt_out) VALUES (1);"
mkdir -p /opt/oa
echo "$ORACLE_B64" | base64 -d > /opt/oa/oracle_service.py
cat > /etc/systemd/system/oa-oracle.service <<UNIT
[Unit]
Description=OpenAdapt AVC444 lab record service
After=docker.service
[Service]
ExecStart=/usr/bin/python3 /opt/oa/oracle_service.py emr 10.30.1.6 8771
Restart=always
[Install]
WantedBy=multi-user.target
UNIT
systemctl daemon-reload
systemctl enable --now oa-oracle
systemctl restart oa-oracle
sleep 2
ss -ltn | grep -q ':8771 ' && echo 'oracle listening' || echo 'oracle NOT listening'
