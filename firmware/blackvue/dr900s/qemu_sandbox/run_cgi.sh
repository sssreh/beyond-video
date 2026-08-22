#!/usr/bin/env bash
# Run one of the DR900S-2CH's extracted ARM CGI binaries directly under
# qemu-arm-static, using the extracted firmware rootfs as the sysroot.
#
# The extracted rootfs already ships its own glibc (eglibc 2.20) and every
# .so the CGI binaries need, so no separate ARM chroot/distro is required -
# pointing qemu-arm-static's -L flag at the rootfs is enough.
#
# Usage:
#   ./run_cgi.sh <rootfs-dir> <binary-name> [QUERY_STRING] [METHOD]
#
# Examples:
#   ./run_cgi.sh /path/to/rootfs blackvue_vod.cgi "action=list"
#   ./run_cgi.sh /path/to/rootfs blackvue_livedata.cgi
#   ./run_cgi.sh /path/to/rootfs boa                     # not a CGI, just runs it
#
# <rootfs-dir> is the "rootfs" folder produced by extracting the firmware:
#   unzip dr900s-2ch-vX.XXX-eng.zip
#   gunzip -c sdcard/BlackVue/System/upgrade/patch_DR900S.bin > patch.tar
#   mkdir rootfs && tar -xf patch.tar -C rootfs
#
# Known CGI binaries live under rootfs/res/System/www/:
#   blackvue_live.cgi, blackvue_livedata.cgi, blackvue_vod.cgi, upload.cgi
# The web server itself and the main app live under rootfs/res/System/:
#   boa, main_app

set -euo pipefail

if [ "$#" -lt 2 ]; then
    echo "Usage: $0 <rootfs-dir> <binary-name> [QUERY_STRING] [METHOD]" >&2
    exit 1
fi

ROOTFS="$1"
BIN_NAME="$2"
QUERY_STRING="${3:-}"
METHOD="${4:-GET}"

if ! command -v qemu-arm-static >/dev/null 2>&1; then
    echo "qemu-arm-static not found on PATH. Run ./setup_sandbox.sh first." >&2
    exit 1
fi

if [ ! -d "$ROOTFS" ]; then
    echo "Rootfs directory not found: $ROOTFS" >&2
    exit 1
fi

# Locate the binary inside the rootfs (checks the common CGI dir first,
# then falls back to a search so this also works for boa/main_app).
BIN_PATH=""
for candidate in \
    "$ROOTFS/res/System/www/$BIN_NAME" \
    "$ROOTFS/res/System/$BIN_NAME"
do
    if [ -f "$candidate" ]; then
        BIN_PATH="$candidate"
        break
    fi
done
if [ -z "$BIN_PATH" ]; then
    BIN_PATH="$(find "$ROOTFS" -type f -name "$BIN_NAME" -print -quit || true)"
fi
if [ -z "$BIN_PATH" ]; then
    echo "Could not find '$BIN_NAME' anywhere under $ROOTFS" >&2
    exit 1
fi

echo "Running: $BIN_PATH"
echo "  sysroot:      $ROOTFS"
echo "  QUERY_STRING: $QUERY_STRING"
echo "  METHOD:       $METHOD"
echo "---"

# Fake the CGI/1.1 environment a real boa web server would set up before
# exec'ing this binary. Real hardware-backed calls (camera stream, GPS,
# g-sensor) have nothing to talk to under emulation and will error out or
# hang - that's expected. This is for exercising the binary's own request
# parsing/handling logic, not for getting a working video stream.
QUERY_LEN=${#QUERY_STRING}
exec qemu-arm-static -L "$ROOTFS" \
    -E LD_LIBRARY_PATH="/usr/lib:/lib" \
    -E GATEWAY_INTERFACE="CGI/1.1" \
    -E SERVER_PROTOCOL="HTTP/1.1" \
    -E SERVER_SOFTWARE="boa/0.94.14rc21" \
    -E REQUEST_METHOD="$METHOD" \
    -E QUERY_STRING="$QUERY_STRING" \
    -E CONTENT_LENGTH="0" \
    -E SCRIPT_NAME="/cgi-bin/$BIN_NAME" \
    -E PATH_INFO="" \
    "$BIN_PATH"
