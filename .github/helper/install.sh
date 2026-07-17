#!/bin/bash
# Mirrors the ERPNext CI install flow: clone Frappe, init a bench, create the
# test DB, pull the dependency apps (payments + erpnext + upande_webshop) and
# this app, then reinstall + install-app. Reproduces a real deploy.
#
# Dependency chain: ecommerce_integration -> upande_webshop -> erpnext + payments.
#
# The DB credentials (root / test_frappe / admin) are the same throwaway values
# ERPNext and HRMS use in their own CI — the MariaDB service is created fresh
# for the run and torn down after it, so nothing here is a real secret.

set -e

cd ~ || exit

sudo apt update
sudo apt remove -y mysql-server mysql-client || true
sudo apt install -y libcups2-dev redis-server mariadb-client libmariadb-dev pkg-config

pip install frappe-bench

frappeuser=${FRAPPE_USER:-"frappe"}
frappebranch=${FRAPPE_BRANCH:-"version-15"}
erpnextbranch=${ERPNEXT_BRANCH:-"version-15"}
paymentsbranch=${PAYMENTS_BRANCH:-"version-15"}
webshoprepo=${WEBSHOP_REPO:-"https://github.com/WilfredTinega/Upande-Webshop.git"}
webshopbranch=${WEBSHOP_BRANCH:-"main"}

git clone "https://github.com/${frappeuser}/frappe" --branch "${frappebranch}" --depth 1
bench init --skip-assets --frappe-path ~/frappe --python "$(which python)" frappe-bench

mkdir ~/frappe-bench/sites/test_site
cp -r "${GITHUB_WORKSPACE}/.github/helper/site_config.json" ~/frappe-bench/sites/test_site/

mariadb --host 127.0.0.1 --port 3306 -u root -proot -e "SET GLOBAL character_set_server = 'utf8mb4'"
mariadb --host 127.0.0.1 --port 3306 -u root -proot -e "SET GLOBAL collation_server = 'utf8mb4_unicode_ci'"

mariadb --host 127.0.0.1 --port 3306 -u root -proot -e "CREATE USER 'test_frappe'@'localhost' IDENTIFIED BY 'test_frappe'"
mariadb --host 127.0.0.1 --port 3306 -u root -proot -e "CREATE DATABASE test_frappe"
mariadb --host 127.0.0.1 --port 3306 -u root -proot -e "GRANT ALL PRIVILEGES ON \`test_frappe\`.* TO 'test_frappe'@'localhost'"

mariadb --host 127.0.0.1 --port 3306 -u root -proot -e "FLUSH PRIVILEGES"

install_wkhtmltopdf() {
    wget -O /tmp/wkhtmltox.tar.xz https://github.com/frappe/wkhtmltopdf/raw/master/wkhtmltox-0.12.3_linux-generic-amd64.tar.xz
    tar -xf /tmp/wkhtmltox.tar.xz -C /tmp
    sudo mv /tmp/wkhtmltox/bin/wkhtmltopdf /usr/local/bin/wkhtmltopdf
    sudo chmod o+x /usr/local/bin/wkhtmltopdf
}
install_wkhtmltopdf &

cd ~/frappe-bench || exit

sed -i 's/watch:/# watch:/g' Procfile
sed -i 's/schedule:/# schedule:/g' Procfile
sed -i 's/socketio:/# socketio:/g' Procfile
sed -i 's/redis_socketio:/# redis_socketio:/g' Procfile

bench get-app "https://github.com/${frappeuser}/payments" --branch "$paymentsbranch"
bench get-app "https://github.com/${frappeuser}/erpnext" --branch "$erpnextbranch" --resolve-deps
bench get-app upande_webshop "$webshoprepo" --branch "$webshopbranch"
bench get-app ecommerce_integration "${GITHUB_WORKSPACE}"
bench setup requirements --dev

bench start &>> ~/frappe-bench/bench_start.log &
CI=Yes bench build --app frappe &
bench --site test_site reinstall --yes

bench --verbose --site test_site install-app ecommerce_integration
