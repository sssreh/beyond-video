# DR900S-2CH qemu-user-static sandbox

A local sandbox for running the DR900S-2CH firmware's extracted ARM
binaries (`boa`, `main_app`, and the four CGI handlers) without a real
camera, using `qemu-arm-static` userspace emulation. Background and the
full investigation that led here (why SSH isn't the answer, why full
`qemu-system-arm` SoC emulation isn't realistic, and why userspace
emulation of just the binaries is) is logged under task #257 in
`WORKING_CONTEXT.md`.

This does **not** emulate the camera's Hisilicon Hi3559 SoC, boot the
real kernel, or produce a working video stream - it runs the plain ARM
ELF userspace programs (ordinary glibc-linked binaries) with their CPU
instructions and Linux syscalls translated to your host, the same
technique firmware-analysis tools like firmadyne/FirmAE use for router
CGI binaries. Good for: exercising a CGI binary's own request parsing,
probing it with malformed input, static analysis (strings/objdump/a
disassembler) with a real process to attach to. Not good for: getting a
live video feed, GPS, or g-sensor data out of it - there's no real
hardware underneath for those calls to reach, so they'll error or hang.

## Prerequisites

Run this on a real Linux machine (native Linux, or WSL2 on Windows) with
normal `sudo`/`apt` access - **not** in a locked-down CI/cowork sandbox.
(This exact limitation is why the scripts here exist rather than a
one-shot cowork session: no root, no apt, and the package proxy blocks
GitHub/PyPI/Debian mirrors outright in that environment.)

## Steps

1. **Extract the firmware**, if you haven't already:

   ```bash
   unzip dr900s-2ch-vX.XXX-eng.zip -d sdcard
   gunzip -c sdcard/BlackVue/System/upgrade/patch_DR900S.bin > patch.tar
   mkdir rootfs && tar -xf patch.tar -C rootfs
   ```

   The CGI binaries end up at `rootfs/res/System/www/*.cgi`, and the web
   server + main app at `rootfs/res/System/{boa,main_app}`.

2. **Install qemu-user-static** (one-time, needs root):

   ```bash
   sudo ./setup_sandbox.sh
   ```

3. **Run a CGI binary directly**, with a faked CGI/1.1 environment:

   ```bash
   ./run_cgi.sh /path/to/rootfs blackvue_vod.cgi "action=list"
   ./run_cgi.sh /path/to/rootfs blackvue_livedata.cgi
   ./run_cgi.sh /path/to/rootfs upload.cgi "" POST
   ```

   `run_cgi.sh` points `qemu-arm-static -L` at the extracted rootfs as
   the sysroot - the firmware ships its own glibc 2.20 and every shared
   library the binaries need (confirmed via `readelf -d`, all four CGI
   binaries only need `libc.so.6`), so no separate ARM distro/chroot has
   to be built.

## Known gaps / things to expect

- **No `boa.conf` was found in the extracted rootfs.** The `boa`
  binary's `strings` output confirms it wants one (`Usage: %s [-c
  serverroot] ... Could not open boa.conf for reading.`), but nothing in
  the SD-card update package ships it - it may be written to flash by
  the camera itself outside the update package, or generated at first
  boot. Running `boa` standalone under this sandbox will likely fail
  until a minimal `boa.conf` is hand-written (a working config for the
  `boa` web server is easy to find online; the CGI/document-root paths
  would need adjusting to match `rootfs/res/System/www/`).
- **The four CGI binaries can be run directly without `boa` at all** -
  that's what `run_cgi.sh` does, and it's the more directly useful path
  for probing their request-handling logic.
- **Hardware-backed calls will fail/hang.** Anything that talks to the
  Hi3559's real MPP video pipeline, GPS receiver, or g-sensor has no
  emulated hardware to talk to. Expect errors, not silent success.
