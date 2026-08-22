#!/usr/bin/env bash
# Cross-compile shim.c into an ARM .so, for LD_PRELOAD-ing into main_app
# under qemu-arm-static (see ../run_main_app.sh).
#
# Needs an ARM cross-compiler. yum/dnf don't package one for RHEL-family
# distros (same story as qemu-user-static itself).
#
# Uses the musl.cc "static cross toolchain" build (arm-linux-musleabi),
# not Arm Ltd's official arm-gnu-toolchain - the official one turned out
# to be a dead end on old hosts: its own binaries are dynamically linked
# against a modern glibc (needs GLIBC_2.27+), which fails outright on
# older distros like Oracle Linux 7 (glibc 2.17). musl.cc's toolchain
# binaries are themselves statically linked (that's the whole point of
# the "static" in "static cross toolchain"), so they carry no dependency
# on the host's glibc version at all and run on virtually any x86_64
# Linux host, however old.
#
# Bonus: arm-linux-musleabi is soft-float, matching the firmware's own
# soft-float EABI exactly (the previous gnueabihf attempt was hard-float,
# which happened to still be fine here since shim.c never touches
# floating point - see the comment at the top of shim.c).
#
# Usage: ./build.sh [output-dir]
#   Produces <output-dir>/gpio_shim.so (default output-dir: this directory)

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
OUT_DIR="${1:-$SCRIPT_DIR}"

TOOLCHAIN_URL="https://more.musl.cc/11.2.1/x86_64-linux-musl/arm-linux-musleabi-cross.tgz"
CACHE_DIR="${HOME}/.cache/arm-musl-toolchain"
TOOLCHAIN_ROOT="${CACHE_DIR}/arm-linux-musleabi-cross"

find_gcc() {
    find "$CACHE_DIR" -maxdepth 4 -type f -name "arm-linux-musleabi-gcc" 2>/dev/null | head -1
}

GCC="$(find_gcc)"

if [ -z "$GCC" ]; then
    echo "ARM cross-compiler not found - downloading it (one-time, ~100MB)..."
    mkdir -p "$CACHE_DIR"
    TMP_TAR="$(mktemp --suffix=.tgz)"
    if command -v curl >/dev/null 2>&1; then
        curl -fL# -o "$TMP_TAR" "$TOOLCHAIN_URL"
    elif command -v wget >/dev/null 2>&1; then
        wget -O "$TMP_TAR" "$TOOLCHAIN_URL"
    else
        echo "Neither curl nor wget found." >&2
        exit 1
    fi
    echo "Extracting..."
    tar -xzf "$TMP_TAR" -C "$CACHE_DIR"
    rm -f "$TMP_TAR"
    GCC="$(find_gcc)"
fi

if [ -z "$GCC" ] || [ ! -x "$GCC" ]; then
    echo "Download/extract succeeded but no arm-linux-musleabi-gcc found under $CACHE_DIR - toolchain layout may have changed upstream." >&2
    echo "Check what actually landed there: find $CACHE_DIR -name '*gcc*'" >&2
    exit 1
fi

echo "Using: $GCC"
"$GCC" --version | head -1

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
