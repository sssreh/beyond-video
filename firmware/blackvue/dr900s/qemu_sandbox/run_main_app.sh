#!/usr/bin/env bash
# Run the DR900S-2CH's main_app under qemu-arm-static with the GPIO ioctl
# shim preloaded, so it can get past the /dev/hi_gpio hardware wall
# documented in README.md.
#
# One-time setup this depends on:
#   1. ./gpio_shim/build.sh          - builds gpio_shim/gpio_shim.so
#   2. sudo mknod <rootfs>/dev/hi_gpio c 1 3
#      sudo chmod 666 <rootfs>/dev/hi_gpio
#
# Usage:
#   ./run_main_app.sh <rootfs-dir>
#
# This is experimental - the shim only fakes success for ioctl() calls on
# /dev/hi_gpio specifically. If main_app gets past its GPIO polling loop,
# whatever it tries to touch next (camera/MPP hardware, most likely) has
# no shim and will fail on its own. That's expected; the point is seeing
# how far it gets and what the next real wall looks like.

set -euo pipefail

if [ "$#" -lt 1 ]; then
    echo "Usage: $0 <rootfs-dir>" >&2
    exit 1
fi

ROOTFS="$1"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SHIM_SO="$SCRIPT_DIR/gpio_shim/gpio_shim.so"
BIN_PATH="$ROOTFS/res/System/main_app"

if ! command -v qemu-arm-static >/dev/null 2>&1; then
    echo "qemu-arm-static not found on PATH. Run ./setup_sandbox.sh first." >&2
    exit 1
fi

if [ ! -f "$BIN_PATH" ]; then
    echo "main_app not found at $BIN_PATH" >&2
    exit 1
fi

if [ ! -f "$SHIM_SO" ]; then
    echo "gpio_shim.so not built yet. Run ./gpio_shim/build.sh first." >&2
    exit 1
fi

if [ ! -c "$ROOTFS/dev/hi_gpio" ]; then
    echo "No fake /dev/hi_gpio device node in the rootfs yet. Create it with:" >&2
    echo "  sudo mknod $ROOTFS/dev/hi_gpio c 1 3" >&2
    echo "  sudo chmod 666 $ROOTFS/dev/hi_gpio" >&2
    exit 1
fi

# Copy the shim .so inside the sysroot so qemu-arm-static's -L resolves it
# the same way it resolves every other library - LD_PRELOAD paths are
# interpreted from the emulated binary's point of view, i.e. relative to
# the sysroot, not the host filesystem.
mkdir -p "$ROOTFS/opt"
cp -f "$SHIM_SO" "$ROOTFS/opt/gpio_shim.so"

echo "Running: $BIN_PATH (with GPIO ioctl shim preloaded)"
echo "  sysroot: $ROOTFS"
echo "---"

exec qemu-arm-static -L "$ROOTFS" \
    -E LD_LIBRARY_PATH="/usr/lib:/lib" \
    -E LD_PRELOAD="/opt/gpio_shim.so" \
    "$BIN_PATH"
