# =============================================================================
# WAA (Windows Agent Arena) Docker Image
# =============================================================================
#
# This image combines:
# 1. dockurr/windows:latest - Modern base that auto-downloads Windows 11
# 2. windowsarena/winarena:latest - Official WAA benchmark client and scripts
#
# The official windowsarena/winarena uses an outdated dockurr/windows (v0.00)
# that doesn't auto-download Windows. This image fixes that while keeping
# full compatibility with the official WAA benchmark.
#
# Usage:
#   # Build the image
#   docker build -t waa-auto:latest .
#
#   # Run the container
#   # CRITICAL: --cap-add NET_ADMIN is REQUIRED for the network bridge between
#   # the Linux container and the Windows QEMU guest. Without it, the container
#   # falls back to user-mode networking and cannot reach the Windows VM at
#   # 20.20.20.21. Port 5000 (WAA Flask server inside Windows) will never be
#   # accessible. Port 5050 (evaluate server on Linux side) will work, which
#   # makes the failure confusing — the container appears to be running but
#   # the WAA server is unreachable.
#   docker run -d --name winarena \
#     --device=/dev/kvm \
#     --cap-add NET_ADMIN \
#     -p 5000:5000 -p 5050:5050 -p 8006:8006 \
#     -v /path/to/storage:/storage \
#     waa-auto:latest
#
# =============================================================================

FROM dockurr/windows:latest

# -----------------------------------------------------------------------------
# Copy official WAA components from windowsarena/winarena
# -----------------------------------------------------------------------------

# Copy benchmark client scripts
COPY --from=windowsarena/winarena:latest /entry.sh /entry.sh
COPY --from=windowsarena/winarena:latest /entry_setup.sh /entry_setup.sh
COPY --from=windowsarena/winarena:latest /start_client.sh /start_client.sh

# Copy the Python benchmark client code
COPY --from=windowsarena/winarena:latest /client /client

# Copy our WAA server startup script
COPY start_waa_server.bat /oem/start_waa_server.bat

# Copy model weights (GroundingDINO, OmniParser, etc.)
COPY --from=windowsarena/winarena:latest /models /models

# Copy Windows setup scripts (install.bat, setup.ps1, etc.)
COPY --from=windowsarena/winarena:latest /oem /oem

# Pre-download LibreOffice MSI into /oem so it's available on the Samba share
# during first boot.  The WAA setup.ps1 downloads from mirrors at runtime which
# frequently times out (or 404s when version is removed), leaving LibreOffice
# uninstalled.  By placing the MSI in /oem it gets shared via Samba to
# \\host.lan\Data\, and we patch setup.ps1 to try the local copy first.
#
# Version discovery: scrapes the stable directory listing at build time so we
# always get whatever version is currently on mirrors.
RUN LO_VER=$(curl -fsSL https://download.documentfoundation.org/libreoffice/stable/ | \
      grep -oP 'href="\K[0-9]+\.[0-9]+\.[0-9]+(?=/")' | sort -V | tail -1) && \
    MSI="LibreOffice_${LO_VER}_Win_x86-64.msi" && \
    echo "Discovered LibreOffice version: $LO_VER ($MSI)" && \
    for url in \
      "https://mirror.raiolanetworks.com/tdf/libreoffice/stable/${LO_VER}/win/x86_64/${MSI}" \
      "https://mirrors.iu13.net/tdf/libreoffice/stable/${LO_VER}/win/x86_64/${MSI}" \
      "https://download.documentfoundation.org/libreoffice/stable/${LO_VER}/win/x86_64/${MSI}"; do \
      echo "Trying $url ..." && \
      curl -fSL --connect-timeout 30 --max-time 300 -o "/oem/${MSI}" "$url" && break || true; \
    done && \
    if ls /oem/LibreOffice_*_Win_x86-64.msi 1>/dev/null 2>&1; then \
      echo "LibreOffice MSI pre-downloaded ($(du -h /oem/${MSI} | cut -f1))"; \
    else \
      echo "WARNING: LibreOffice MSI download failed from all mirrors (install will retry at first boot)"; \
    fi

# Patch setup.ps1 to install LibreOffice from the pre-downloaded MSI on the
# Samba share if it exists, falling through to the original mirror download
# otherwise.  Uses a shell script (no python3 in the base image).
COPY patch_setup_ps1.sh /tmp/patch_setup_ps1.sh
RUN sh /tmp/patch_setup_ps1.sh /oem/setup.ps1 && rm -f /tmp/patch_setup_ps1.sh

# Copy OEM files AFTER dockurr/samba starts (which wipes /tmp/smb)
# Copy IMMEDIATELY (no delay) and SYNCHRONOUSLY (not backgrounded) to ensure
# files are available before Windows boots and runs FirstLogonCommands
RUN sed -i '/^return 0$/i cp -r /oem/* /tmp/smb/ 2>/dev/null || true' /run/samba.sh && \
    echo "Inserted OEM copy before return in samba.sh"

# DO NOT replace dockurr/windows's autounattend.xml - it handles OOBE properly
# Instead, only PATCH it to add InstallFrom element (prevents "Select OS" dialog)
# This preserves dockurr/windows's native OEM mechanism
RUN for xml in /run/assets/win11x64.xml /run/assets/win11x64-enterprise-eval.xml; do \
        if [ -f "$xml" ] && ! grep -q "InstallFrom" "$xml"; then \
            sed -i 's|<InstallTo>|<InstallFrom>\n            <MetaData wcm:action="add">\n              <Key>/IMAGE/INDEX</Key>\n              <Value>1</Value>\n            </MetaData>\n          </InstallFrom>\n          <InstallTo>|' "$xml"; \
        fi; \
    done && echo "Added InstallFrom element for automatic image selection"

# -----------------------------------------------------------------------------
# Create start_vm.sh that uses our dockurr/windows entrypoint
# -----------------------------------------------------------------------------

RUN printf '#!/bin/bash\n/usr/bin/tini -s /run/entry.sh\n' > /start_vm.sh && chmod +x /start_vm.sh

# -----------------------------------------------------------------------------
# Patch IP addresses: official uses 20.20.20.21, dockurr/windows uses 172.30.0.2
# -----------------------------------------------------------------------------

# Patch entry scripts (must work - these files were just copied)
RUN sed -i 's|20.20.20.21|172.30.0.2|g' /entry_setup.sh && \
    sed -i 's|20.20.20.21|172.30.0.2|g' /entry.sh && \
    sed -i 's|20.20.20.21|172.30.0.2|g' /start_client.sh && \
    echo "Patched entry scripts"

# Patch client Python files
RUN find /client -name "*.py" -exec sed -i 's|20.20.20.21|172.30.0.2|g' {} \; && \
    echo "Patched client Python files"

# Add timeout to OpenAI API calls (prevents infinite hangs)
RUN sed -i 's|openai.OpenAI(api_key=self.api_key)|openai.OpenAI(api_key=self.api_key, timeout=120.0)|' \
    /client/mm_agents/navi/gpt/gpt4v_oai.py && \
    echo "Patched OpenAI client with 120s timeout"

# -----------------------------------------------------------------------------
# Add API-backed agent support (Claude / OpenAI)
# NOTE: API agents (api-claude, api-openai) are run EXTERNALLY via openadapt-evals CLI
# which connects to the WAA server over SSH tunnel. No internal patching needed.
# The api_agent.py is included for reference/future use.
# -----------------------------------------------------------------------------

# Copy api_agent.py for reference (used externally by openadapt-evals)
COPY api_agent.py /client/mm_agents/api_agent.py

# -----------------------------------------------------------------------------
# Deploy evaluate server (runs on Docker Linux side, NOT inside Windows)
# The evaluate server imports WAA evaluator modules from /client/desktop_env/
# and connects to the Windows VM at 172.30.0.2:5000 via PythonController.
# It exposes /evaluate and /task/<task_id> endpoints on port 5050.
# -----------------------------------------------------------------------------

COPY evaluate_server.py /evaluate_server.py

# Verify evaluate_server.py was copied correctly (not a symlink or empty file).
# A symlink to /proc/self/fd/0 has been observed when the build context is
# missing the file or Docker copies from stdin.  Catch this at build time.
RUN set -e && \
    if [ -L /evaluate_server.py ]; then \
        echo "FATAL: /evaluate_server.py is a symlink ($(readlink /evaluate_server.py)), not a regular file." >&2; \
        echo "This means the Docker build context was missing evaluate_server.py." >&2; \
        exit 1; \
    fi && \
    if [ ! -s /evaluate_server.py ]; then \
        echo "FATAL: /evaluate_server.py is empty or missing." >&2; \
        exit 1; \
    fi && \
    for route in '/probe' '/task/' '/setup' '/evaluate'; do \
        if ! grep -q "$route" /evaluate_server.py; then \
            echo "FATAL: /evaluate_server.py missing expected route: $route" >&2; \
            exit 1; \
        fi; \
    done && \
    echo "evaluate_server.py verified: $(wc -l < /evaluate_server.py) lines, all routes present"

# Install flask for the evaluate server and socat for port forwarding
RUN pip install flask requests-toolbelt 2>/dev/null || pip3 install flask requests-toolbelt 2>/dev/null || \
    python -m pip install flask requests-toolbelt 2>/dev/null || \
    echo "WARNING: flask/requests-toolbelt install failed, evaluate server may not work"
RUN apt-get update -qq && apt-get install -y -qq socat && rm -rf /var/lib/apt/lists/*

# -----------------------------------------------------------------------------
# Fix Windows setup for automation
# -----------------------------------------------------------------------------

# Set password for AutoLogon (Windows 11 requires password for login)
RUN sed -i 's|<Value></Value>|<Value>docker</Value>|g' /run/assets/win11x64.xml 2>/dev/null || true
RUN sed -i 's|<Value />|<Value>docker</Value>|g' /run/assets/win11x64.xml 2>/dev/null || true

# Add firewall disable and other automation commands to FirstLogonCommands
# CRITICAL: Also create a scheduled task so WAA server starts on EVERY boot, not just first logon
RUN if grep -q "</FirstLogonCommands>" /run/assets/win11x64.xml; then \
    LAST_ORDER=$(grep -oP "Order>\K[0-9]+" /run/assets/win11x64.xml | sort -n | tail -1) && \
    N1=$((LAST_ORDER + 1)) && \
    N2=$((LAST_ORDER + 2)) && \
    N3=$((LAST_ORDER + 3)) && \
    N4=$((LAST_ORDER + 4)) && \
    N5=$((LAST_ORDER + 5)) && \
    N6=$((LAST_ORDER + 6)) && \
    sed -i "s|</FirstLogonCommands>|\
        <SynchronousCommand wcm:action=\"add\">\n\
          <Order>$N1</Order>\n\
          <CommandLine>netsh advfirewall set allprofiles state off</CommandLine>\n\
          <Description>Disable Windows Firewall</Description>\n\
        </SynchronousCommand>\n\
        <SynchronousCommand wcm:action=\"add\">\n\
          <Order>$N2</Order>\n\
          <CommandLine>powercfg /change standby-timeout-ac 0</CommandLine>\n\
          <Description>Disable sleep</Description>\n\
        </SynchronousCommand>\n\
        <SynchronousCommand wcm:action=\"add\">\n\
          <Order>$N3</Order>\n\
          <CommandLine>powercfg /change monitor-timeout-ac 0</CommandLine>\n\
          <Description>Disable monitor timeout</Description>\n\
        </SynchronousCommand>\n\
        <SynchronousCommand wcm:action=\"add\">\n\
          <Order>$N4</Order>\n\
          <CommandLine>reg add \"HKLM\\\\SOFTWARE\\\\Policies\\\\Microsoft\\\\Windows\\\\Personalization\" /v NoLockScreen /t REG_DWORD /d 1 /f</CommandLine>\n\
          <Description>Disable lock screen</Description>\n\
        </SynchronousCommand>\n\
        <SynchronousCommand wcm:action=\"add\">\n\
          <Order>$N5</Order>\n\
          <CommandLine>cmd /c start /wait \\\\\\\\host.lan\\\\Data\\\\install.bat</CommandLine>\n\
          <Description>Run WAA setup script to install Python, Chrome, etc.</Description>\n\
        </SynchronousCommand>\n\
        <SynchronousCommand wcm:action=\"add\">\n\
          <Order>$N6</Order>\n\
          <CommandLine>schtasks /create /tn \"WAAServer\" /tr \"\\\\\\\\host.lan\\\\Data\\\\start_waa_server.bat\" /sc onlogon /rl highest /f</CommandLine>\n\
          <Description>Create scheduled task for WAA server auto-start on every boot</Description>\n\
        </SynchronousCommand>\n\
        <SynchronousCommand wcm:action=\"add\">\n\
          <Order>$((N6 + 1))</Order>\n\
          <CommandLine>reg add \"HKCU\\\\SOFTWARE\\\\Microsoft\\\\Windows\\\\CurrentVersion\\\\Run\" /v WAAServer /t REG_SZ /d \"cmd /c \\\\\\\\host.lan\\\\Data\\\\start_waa_server.bat\" /f</CommandLine>\n\
          <Description>Add registry entry for WAA server auto-start (backup)</Description>\n\
        </SynchronousCommand>\n\
        <SynchronousCommand wcm:action=\"add\">\n\
          <Order>$((N6 + 2))</Order>\n\
          <CommandLine>\\\\\\\\host.lan\\\\Data\\\\start_waa_server.bat</CommandLine>\n\
          <Description>Start WAA server immediately</Description>\n\
        </SynchronousCommand>\n\
        <SynchronousCommand wcm:action=\"add\">\n\
          <Order>$((N6 + 3))</Order>\n\
          <CommandLine>bcdedit /set {default} recoveryenabled No</CommandLine>\n\
          <Description>Disable Windows Automatic Repair to prevent recovery loops after dirty shutdown</Description>\n\
        </SynchronousCommand>\n\
        <SynchronousCommand wcm:action=\"add\">\n\
          <Order>$((N6 + 4))</Order>\n\
          <CommandLine>reg add \"HKLM\\\\SOFTWARE\\\\Policies\\\\Google\\\\Chrome\" /v BrowserSignin /t REG_DWORD /d 0 /f</CommandLine>\n\
          <Description>Disable Chrome sign-in prompt on first run</Description>\n\
        </SynchronousCommand>\n\
        <SynchronousCommand wcm:action=\"add\">\n\
          <Order>$((N6 + 5))</Order>\n\
          <CommandLine>reg add \"HKLM\\\\SOFTWARE\\\\Policies\\\\Google\\\\Chrome\" /v SyncDisabled /t REG_DWORD /d 1 /f</CommandLine>\n\
          <Description>Disable Chrome sync to prevent first-run sync dialog</Description>\n\
        </SynchronousCommand>\n\
        <SynchronousCommand wcm:action=\"add\">\n\
          <Order>$((N6 + 6))</Order>\n\
          <CommandLine>reg add \"HKLM\\\\SOFTWARE\\\\Policies\\\\Google\\\\Chrome\" /v PromotionalTabsEnabled /t REG_DWORD /d 0 /f</CommandLine>\n\
          <Description>Disable Chrome promotional tabs (What is New page)</Description>\n\
        </SynchronousCommand>\n\
      </FirstLogonCommands>|" /run/assets/win11x64.xml; \
    fi

# -----------------------------------------------------------------------------
# Copy Python 3.9 and all packages from vanilla image
# -----------------------------------------------------------------------------
# IMPORTANT: Do NOT install Python from apt or pip install packages ourselves.
# The vanilla image has Python 3.9.20 with transformers 4.46.2 which is compatible
# with GroundingDINO. Installing our own Python (3.13) with latest transformers (5.0)
# breaks the navi agent with: AttributeError: 'BertModel' has no attribute 'get_head_mask'

# Copy Python 3.9 installation from vanilla (binaries, libraries, packages)
COPY --from=windowsarena/winarena:latest /usr/local/bin/python* /usr/local/bin/
COPY --from=windowsarena/winarena:latest /usr/local/bin/pip* /usr/local/bin/
COPY --from=windowsarena/winarena:latest /usr/local/lib/python3.9 /usr/local/lib/python3.9
COPY --from=windowsarena/winarena:latest /usr/local/lib/libpython3.9.so* /usr/local/lib/
COPY --from=windowsarena/winarena:latest /usr/local/include/python3.9 /usr/local/include/python3.9

# Ensure the shared library is found
RUN ldconfig

# Create symlinks for python/pip commands
RUN ln -sf /usr/local/bin/python3.9 /usr/local/bin/python && \
    ln -sf /usr/local/bin/python3.9 /usr/bin/python && \
    ln -sf /usr/local/bin/python3.9 /usr/bin/python3 && \
    ln -sf /usr/local/bin/pip3.9 /usr/local/bin/pip && \
    ln -sf /usr/local/bin/pip3.9 /usr/bin/pip && \
    ln -sf /usr/local/bin/pip3.9 /usr/bin/pip3

# Install only system dependencies that Python packages need (not Python itself)
RUN apt-get update && apt-get install -y --no-install-recommends \
    tesseract-ocr \
    libgl1 \
    libglib2.0-0 \
    libsm6 \
    libxext6 \
    libxrender-dev \
    ffmpeg \
    && rm -rf /var/lib/apt/lists/*

# Note: Playwright browsers not copied - not needed for navi agent (uses GroundingDINO)
# If needed later, install via: python -m playwright install chromium

# -----------------------------------------------------------------------------
# Environment configuration
# -----------------------------------------------------------------------------

ENV YRES="900"
ENV XRES="1440"
ENV RAM_SIZE="8G"
ENV CPU_CORES="4"
ENV DISK_SIZE="30G"
ENV VERSION="11e"
ENV ARGUMENTS="-qmp tcp:0.0.0.0:7200,server,nowait"

# Expose ports
EXPOSE 8006 5000 5050 7200 3389

# -----------------------------------------------------------------------------
# Entrypoint: start evaluate server + dockurr/windows entry point
# The evaluate server runs on port 5050 and provides /evaluate + /task/<id>
# endpoints using WAA's evaluator modules from the Docker Linux side.
# -----------------------------------------------------------------------------

COPY start_with_evaluate.sh /run/start_with_evaluate.sh
RUN chmod +x /run/start_with_evaluate.sh

ENTRYPOINT ["/usr/bin/tini", "-s", "--", "/run/start_with_evaluate.sh"]
CMD ["/entry.sh", "--prepare-image", "false", "--start-client", "false"]
