# Real-RDP vision-ladder fixture.
#
# A self-contained, CI-viable RDP round-trip: a deterministic Tk kiosk app is
# served over the RDP wire by a FreeRDP *server* (freerdp-shadow-cli3) and
# rendered back by a FreeRDP *client* (xfreerdp3) into a second headless
# display. The harness screenshots that client display (RDP-wire-decoded pixels)
# and injects input into it (forwarded over the RDP wire to the kiosk), so both
# directions traverse a genuine RDP protocol exchange.
#
# Why FreeRDP3 both ends: the product's aardwolf client cannot interoperate with
# Linux RDP servers (xrdp does not send the MCS tokenInhibitConfirm aardwolf
# waits for; FreeRDP3 rejects aardwolf's off-by-one MCS Erect-Domain TPKT), so a
# CI-viable Linux RDP surface for the *vision ladder* must use a conformant
# client/server pair. This validates the resolver ladder + contract over real
# RDP pixels; the aardwolf transport itself is separately qualified against
# Windows RDP (benchmark/rdp, PR #142). See docs/backends/RDP.md and the design
# note in this directory's README.
#
# Multi-arch base (ubuntu:24.04 = amd64 + arm64) so it runs natively on
# Apple-Silicon dev machines AND amd64 CI runners.
FROM ubuntu:24.04@sha256:4fbb8e6a8395de5a7550b33509421a2bafbc0aab6c06ba2cef9ebffbc7092d90

RUN apt-get update \
 && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
      xvfb x11-utils python3-tk fonts-dejavu-core \
      freerdp3-shadow-x11 freerdp3-x11 \
      imagemagick xdotool openbox procps \
 && rm -rf /var/lib/apt/lists/*

COPY kiosk_app.py /opt/rdp_fixture/kiosk_app.py
COPY run_fixture.sh /opt/rdp_fixture/run_fixture.sh
RUN chmod +x /opt/rdp_fixture/run_fixture.sh && mkdir -p /opt/rdp_fixture

# The image is built and run only as an ephemeral qualification fixture. The
# apt-installed FreeRDP/Openbox/xdotool/ImageMagick binaries are not copied into
# the Python wheel or sdist and this image is not published by the workflow.
# ImageMagick's default policy permits the X-window-to-PNG capture used here.
EXPOSE 3389
CMD ["/opt/rdp_fixture/run_fixture.sh"]
