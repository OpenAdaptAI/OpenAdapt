# Trac 1.6 (BSD-3-Clause) for the API-step generalization benchmark: a synthetic
# local service desk. Throwaway admin password; bind to 127.0.0.1 only.
FROM docker.io/library/python:3.12-slim-bookworm
RUN pip install --no-cache-dir "Trac==1.6" "Jinja2<3.2" "Babel" "setuptools<81" && trac-admin /srv/trac initenv "Synthetic Service Desk" sqlite:db/trac.db \
 && printf 'admin:{SHA}%s\n' "$(python -c 'import hashlib,base64;print(base64.b64encode(hashlib.sha1(b"synthetic-admin-pass").digest()).decode())')" > /srv/trac/htpasswd \
 && trac-admin /srv/trac permission add admin TRAC_ADMIN
EXPOSE 8000
CMD ["tracd", "-s", "--port", "8000", "--basic-auth", "*,/srv/trac/htpasswd,trac", "/srv/trac"]
