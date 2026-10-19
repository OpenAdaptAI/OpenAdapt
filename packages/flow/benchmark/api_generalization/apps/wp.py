"""WordPress 6.6 (CMS, PHP, MariaDB): the shared fixture for the WordPress workflows.

Runs as an external service from the official images; nothing from
WordPress (GPL-2.0) is copied here. The one file written into the container
is this harness's own must-use plugin that turns off WordPress's comment
flood throttle (a test copy posts many comments from one address).
"""

from __future__ import annotations

import subprocess
import time
from typing import Any

from benchmark.api_generalization.common import AppBase, mysql_rows, podman

POD = "apigen-wp"
DB_C = "apigen-wp-db"
WP_C = "apigen-wp-app"
WP_IMAGE = "docker.io/library/wordpress:6.6-php8.2-apache"
DB_IMAGE = "docker.io/library/mariadb@sha256:efb4959ef2c835cd735dbc388eb9ad6aab0c78dd64febcd51bc17481111890c4"
DB_PASS = "synthetic-db-pass"  # throwaway local container
ADMIN_PASS = "synthetic-admin-pass"  # throwaway local container
UPSTREAM, PROXY = 9831, 9832
MU_PLUGIN = "<?php\n// Test copy only: many comments come from one address.\nadd_filter('comment_flood_filter', '__return_false');\n"


def wpsql(query: str) -> list[dict[str, Any]]:
    return mysql_rows(DB_C, "root", DB_PASS, "wordpress", query)


class WordPressBase(AppBase):
    upstream_port = UPSTREAM
    proxy_port = PROXY

    def up(self) -> None:
        if subprocess.run(["podman", "pod", "exists", POD]).returncode == 0:
            podman("pod", "start", POD)
            self.wait_http("/wp-login.php")
            return
        podman("pod", "create", "--name", POD, "-p", f"127.0.0.1:{UPSTREAM}:80")
        podman("run", "-d", "--pod", POD, "--name", DB_C, "-e", f"MARIADB_ROOT_PASSWORD={DB_PASS}",
               "-e", "MARIADB_DATABASE=wordpress", DB_IMAGE)
        podman("run", "-d", "--pod", POD, "--name", WP_C, "-e", "WORDPRESS_DB_HOST=127.0.0.1",
               "-e", "WORDPRESS_DB_USER=root", "-e", f"WORDPRESS_DB_PASSWORD={DB_PASS}",
               "-e", "WORDPRESS_DB_NAME=wordpress", WP_IMAGE)
        self.wait_http("/wp-admin/install.php")
        import urllib.parse
        import urllib.request

        body = urllib.parse.urlencode({
            "weblog_title": "Synthetic Community Notes", "user_name": "admin", "admin_password": ADMIN_PASS,
            "admin_password2": ADMIN_PASS, "pw_weak": "1", "admin_email": "admin@example.invalid",
            "blog_public": "0", "language": ""}).encode()
        urllib.request.urlopen(f"http://127.0.0.1:{UPSTREAM}/wp-admin/install.php?step=2", data=body, timeout=120).read()
        base = f"http://127.0.0.1:{PROXY}"
        wpsql(f"UPDATE wp_options SET option_value='{base}' WHERE option_name IN ('siteurl','home')")
        podman("exec", "-i", WP_C, "sh", "-c",
               "mkdir -p /var/www/html/wp-content/mu-plugins && cat > /var/www/html/wp-content/mu-plugins/apigen-test-copy.php",
               input=MU_PLUGIN)
        time.sleep(1)

    def down(self) -> None:
        podman("pod", "stop", POD, check=False)

    def login(self, page: Any) -> None:
        page.goto(self.base + "/wp-login.php")
        page.fill("#user_login", "admin")
        page.fill("#user_pass", ADMIN_PASS)
        page.click("#wp-submit")
        page.wait_for_load_state("load")

    def expire_session(self, page: Any) -> None:
        # WordPress keeps each login's session token in usermeta; deleting it
        # ends every session on the server.
        wpsql("DELETE FROM wp_usermeta WHERE meta_key = 'session_tokens'")
