#!/usr/bin/env bash
# One-time host setup for the DR900S-2CH qemu-user-static sandbox.
#
# Run this on your own Linux machine (native Linux, or WSL2 on Windows) -
# it needs real root access, which a locked-down cowork/CI sandbox will
# not have. See ../README.md for the full walkthrough and background on
# why this exists (task #257 in WORKING_CONTEXT.md).
#
# Tries, in order:
#   1. apt-get install qemu-user-static (Debian/Ubuntu)
#   2. dnf/yum install qemu-user-static (Fedora, and EL7 with EPEL enabled -
#      NOT packaged for RHEL/CentOS/Rocky/Alma 8+, even in EPEL, as of this
#      writing: https://access.redhat.com/solutions/5654221)
#   3. Fallback: download the prebuilt static binary directly from the
#      multiarch/qemu-user-static GitHub releases - works on ANY distro,
#      including RHEL-family yum/dnf systems where the package isn't
#      available at all.
#
# This does NOT touch a real camera and does NOT need one connected.

set -euo pipefail

if [ "$(id -u)" -ne 0 ]; then
    echo "This script needs root (sudo) to install packages." >&2
    echo "Re-run as: sudo $0" >&2
    exit 1
fi

install_via_binary_download() {
    echo "Falling back to a direct static-binary download (no package manager needed)..."
    local url="https://github.com/multiarch/qemu-user-static/releases/latest/download/qemu-arm-static"
    local dest="/usr/local/bin/qemu-arm-static"
    if command -v curl >/dev/null 2>&1; then
        curl -fsSL -o "$dest" "$url"
    elif command -v wget >/dev/null 2>&1; then
        wget -q -O "$dest" "$url"
    else
        echo "Neither curl nor wget found - can't download the fallback binary." >&2
        return 1
    fi
    chmod +x "$dest"
}

if command -v apt-get >/dev/null 2>&1; then
    apt-get update
    apt-get install -y qemu-user-static binfmt-support || install_via_binary_download
elif command -v dnf >/dev/null 2>&1; then
    dnf install -y qemu-user-static || install_via_binary_download
elif command -v yum >/dev/null 2>&1; then
    yum install -y qemu-user-static || install_via_binary_download
else
    echo "No apt-get/dnf/yum found - going straight to the binary download." >&2
    install_via_binary_download
fi

echo
echo "Done. Verifying..."
if command -v qemu-arm-static >/dev/null 2>&1; then
    qemu-arm-static -version | head -1
    echo "qemu-arm-static is ready. Next: run ./run_cgi.sh"
else
    echo "qemu-arm-static still not on PATH - check the install output above." >&2
    echo "You can also try the fallback manually:" >&2
    echo "  curl -fsSL -o /usr/local/bin/qemu-arm-static \\" >&2
    echo "    https://github.com/multiarch/qemu-user-static/releases/latest/download/qemu-arm-static" >&2
    echo "  chmod +x /usr/local/bin/qemu-arm-static" >&2
    exit 1
fi
