# No-DOM HTML5-canvas vision-ladder fixture (Citrix Workspace-web CLASS analog).
#
# Presents the deterministic Tk "Patient Notes" kiosk as a genuine no-accessible
# -DOM pixel CANVAS -- the SAME class of surface Citrix Workspace-*web* presents
# (an HTML5 <canvas> painted by a remote session over a WebSocket, with no
# accessible content inside the canvas). This is a legitimate stand-in for the
# canvas class; it is NOT Citrix ICA/HDX (no HDX codecs, no ICA compression, no
# Workspace-client input path). See ../README.md for the honest scope boundary.
#
# Stack (all repository-only fixture image; nothing here is packaged in the
# openadapt-flow wheel/sdist -- benchmark/ is excluded from the build):
#   * TigerVNC standalone Xvnc  -> IS the X display :0 AND a VNC server on :5900
#   * the Tk kiosk              -> runs fullscreen on :0 (the "remote session")
#   * noVNC + websockify        -> render :0's VNC framebuffer into an HTML5
#                                  <canvas> served at http://<host>:6080
# The harness drives the noVNC <canvas> with a browser backend (Playwright):
# screenshots are the canvas pixels; clicks/keys are injected at canvas-relative
# pixel coordinates and forwarded VNC -> Xvnc -> kiosk. Both directions cross a
# real remote-display (VNC) exchange rendered onto a no-DOM canvas.
#
# License posture: noVNC is MPL-2.0, websockify LGPL-3, TigerVNC GPL-2.0. These
# are apt-installed EXTERNAL applications run at test time inside a repo-only
# fixture image (analogous to automating an external app), NOT vendored source
# in the distributed package -- consistent with AGENTS.md "License Hygiene and
# Package Boundaries".
#
# Multi-arch base (ubuntu:24.04 = amd64 + arm64) so it runs natively on
# Apple-Silicon dev machines AND amd64 CI runners.
FROM ubuntu:24.04

RUN apt-get update \
 && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
      tigervnc-standalone-server tigervnc-common \
      novnc websockify \
      python3 python3-tk fonts-dejavu-core \
      x11-utils procps \
 && rm -rf /var/lib/apt/lists/*

COPY kiosk_app.py /opt/canvas_fixture/kiosk_app.py
COPY run_fixture.sh /opt/canvas_fixture/run_fixture.sh
RUN chmod +x /opt/canvas_fixture/run_fixture.sh && mkdir -p /opt/canvas_fixture

# 6080 = noVNC/websockify HTTP+WebSocket (the canvas surface the harness drives).
EXPOSE 6080
CMD ["/opt/canvas_fixture/run_fixture.sh"]
