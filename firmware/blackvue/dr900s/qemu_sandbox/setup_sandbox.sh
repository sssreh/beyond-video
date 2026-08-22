#!/usr/bin/env bash
# One-time host setup for the DR900S-2CH qemu-user-static sandbox.
#
# Run this on your own Linux machine (native Linux, or WSL2 on Windows) -
# it needs real apt + root access, which a locked-down cowork/CI sandbox
# will not have. See ../README.md for the full walkthrough and background
# on why this exists (task #257 in WORKING_CONTEXT.md).
#
# What this installs:
#   qemu-user-static  - userspace ARM CPU emulation (qemu-arm-static)
#   binfmt-support     - optional, only needed if you want the kernel to
#                         auto-dispatch ARM binaries to qemu transparently
#                         (not required for the direct-invocation workflow
#                         used by run_cgi.sh)
#
# This does NOT touch a real camera and does NOT need one connected.

set -euo pipefail

if [ "$(id -u)" -ne 0 ]; then
    echo "This script needs root (sudo) to install packages." >&2
    echo "Re-run as: sudo $0" >&2
    exit 1
fi

if ! command -v apt-get >/dev/null 2>&1; then
    echo "apt-get not found. This script targets Debian/Ubuntu." >&2
    echo "On other distros, install the equivalent of qemu-user-static" >&2
    echo "by hand (Fedora: qemu-user-static; Arch: qemu-user-static-bin)." >&2
    exit 1
fi

apt-get update
apt-get install -y qemu-user-static binfmt-support

echo
echo "Done. Verifying..."
if command -v qemu-arm-static >/dev/null 2>&1; then
    qemu-arm-static -version | head -1
    echo "qemu-arm-static is ready. Next: run ./run_cgi.sh"
else
    echo "qemu-arm-static still not on PATH - check the apt install output above." >&2
    exit 1
fi
