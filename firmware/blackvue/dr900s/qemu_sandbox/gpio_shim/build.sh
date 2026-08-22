#!/usr/bin/env bash
# Cross-compile shim.c into an ARM .so, for LD_PRELOAD-ing into main_app
# under qemu-arm-static (see ../run_main_app.sh).
#
# Needs an ARM cross-compiler. yum/dnf don't package one for RHEL-family
# distros (same story as qemu-user-static itself), so this downloads Arm
# Ltd's own prebuilt toolchain (arm-none-linux-gnueabihf) straight from
# developer.arm.com the first time it's run, and reuses it after that.
#
# The toolchain is hard-float (gnueabihf) while the firmware itself is
# soft-float (gnueabi) - that's fine here because shim.c never touches
# floating point and links against nothing but its own raw syscalls (see
# the comment at the top of shim.c for why). Don't reuse this toolchain
# for anything that DOES use floats without checking ABI compatibility
# first.
#
# Usage: ./build.sh [output-dir]
#   Produces <output-dir>/gpio_shim.so (default output-dir: this directory)

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
OUT_DIR="${1:-$SCRIPT_DIR}"

TOOLCHAIN_VERSION="15.2.rel1"
TOOLCHAIN_NAME="arm-gnu-toolchain-${TOOLCHAIN_VERSION}-x86_64-arm-none-linux-gnueabihf"
TOOLCHAIN_URL="https://developer.arm.com/-/media/Files/downloads/gnu/${TOOLCHAIN_VERSION}/binrel/${TOOLCHAIN_NAME}.tar.xz"
CACHE_DIR="${HOME}/.cache/arm-gnu-toolchain"
TOOLCHAIN_DIR="${CACHE_DIR}/${TOOLCHAIN_NAME}"
GCC="${TOOLCHAIN_DIR}/bin/arm-none-linux-gnueabihf-gcc"

if [ ! -x "$GCC" ]; then
    echo "ARM cross-compiler not found at $GCC - downloading it (one-time, ~150MB)..."
    mkdir -p "$CACHE_DIR"
    TMP_TAR="$(mktemp --suffix=.tar.xz)"
    if command -v curl >/dev/null 2>&1; then
        curl -fL# -o "$TMP_TAR" "$TOOLCHAIN_URL"
    elif command -v wget >/dev/null 2>&1; then
        wget -O "$TMP_TAR" "$TOOLCHAIN_URL"
    else
        echo "Neither curl nor wget found." >&2
        exit 1
    fi
    echo "Extracting..."
    tar -xf "$TMP_TAR" -C "$CACHE_DIR"
    rm -f "$TMP_TAR"
fi

if [ ! -x "$GCC" ]; then
    echo "Download/extract succeeded but $GCC still isn't there - toolchain layout may have changed upstream." >&2
    echo "Check what actually landed in $CACHE_DIR and adjust TOOLCHAIN_NAME/GCC above." >&2
    exit 1
fi

echo "Using: $("$GCC" --version | head -1)"

mkdir -p "$OUT_DIR"
"$GCC" \
    -fPIC -shared \
    -nostdlib -nostartfiles \
    -fno-stack-protector -fno-builtin \
    -marm -fomit-frame-pointer \
    -Wall -Wextra \
    -o "$OUT_DIR/gpio_shim.so" \
    "$SCRIPT_DIR/shim.c"

echo "Built: $OUT_DIR/gpio_shim.so"
file "$OUT_DIR/gpio_shim.so" 2>/dev/null || true
