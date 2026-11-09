FROM ubuntu:24.04@sha256:4fbb8e6a8395de5a7550b33509421a2bafbc0aab6c06ba2cef9ebffbc7092d90

RUN apt-get update \
 && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
      xvfb x11-utils python3-tk fonts-dejavu-core \
      freerdp3-shadow-x11 freerdp3-x11 \
      imagemagick xdotool openbox procps \
 && rm -rf /var/lib/apt/lists/*

COPY suite_app.py /opt/rdp_multiapp/suite_app.py
COPY run_fixture.sh /opt/rdp_multiapp/run_fixture.sh
RUN chmod +x /opt/rdp_multiapp/run_fixture.sh \
 && mkdir -p /opt/rdp_multiapp/oracle

EXPOSE 3389
CMD ["/opt/rdp_multiapp/run_fixture.sh"]
