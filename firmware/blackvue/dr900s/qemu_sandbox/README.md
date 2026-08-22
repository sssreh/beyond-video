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
normal root access - **not** in a locked-down CI/cowork sandbox. (This
exact limitation is why the scripts here exist rather than a one-shot
cowork session: no root, no apt/yum, and the package proxy blocks
GitHub/PyPI/Debian mirrors outright in that environment.)

`setup_sandbox.sh` works on both apt-based (Debian/Ubuntu) and
yum/dnf-based (Fedora, RHEL, CentOS, Rocky, Alma) systems. On the
RHEL family specifically, `qemu-user-static` isn't packaged for 8+ even
via EPEL (confirmed via Red Hat's own docs), so the script automatically
falls back to downloading a prebuilt static `qemu-arm-static` binary
straight from the [multiarch/qemu-user-static GitHub
releases](https://github.com/multiarch/qemu-user-static/releases) if the
package install fails or no package manager is found at all - this
fallback works on any distro, yum-based or not.

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
   to be built. It also sets `LD_LIBRARY_PATH=/usr/lib:/lib` (inside the
   sysroot) since the default dynamic-linker search path doesn't always
   pick up custom libs like `libmwlog.so` on every host otherwise.

   Confirmed working on Oracle Linux (yum-based) against firmware
   v1.009: `upload.cgi` runs cleanly and returns a real HTML response
   (its upload form) with no dependencies beyond libc. `blackvue_vod.cgi`,
   `blackvue_live.cgi`, and `blackvue_livedata.cgi` all print `shmget
   failed` and exit - they expect a System V shared-memory segment that
   the camera's `main_app` process normally creates to share live
   video/status state, which doesn't exist without it running too. Trying
   to run `main_app` alongside doesn't get further in practice - it needs
   real Hi3559 MPP/camera hardware and fails immediately once its own
   dependencies resolve. This confirms the sandbox's core promise: real
   ARM firmware binaries execute and produce real output under emulation,
   with the boundary being camera-hardware-backed calls specifically, not
   the emulation itself.

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
- **`main_app`'s GPIO check was a real wall, but a beatable one - see
  `gpio_shim/`.** `main_app` fails with `open GPIO_DEV err` trying to
  open `/dev/hi_gpio` (found via `strings`). A fake character device
  node (`sudo mknod rootfs/dev/hi_gpio c 1 3 && sudo chmod 666
  rootfs/dev/hi_gpio`) gets past `open()`, but the next call -
  `HI_HAL_Gpio_GetBitVal`'s `ioctl()` - originally failed with `read
  gpio data failed`, since a device node backed by `/dev/null`'s
  major/minor doesn't implement the ioctl the code expects.

  `gpio_shim/` fixes this properly: an `LD_PRELOAD` shim
  (`gpio_shim.so`, built by `gpio_shim/build.sh`) that intercepts every
  `ioctl()` call and fakes success specifically for fds pointing at
  `/dev/hi_gpio`, leaving every other ioctl untouched. Wire it in with
  `run_main_app.sh`. Confirmed working end-to-end on Oracle Linux
  against firmware v1.009: `main_app` clears the GPIO loop completely
  (four real `[gpio-shim] faking ioctl() success` lines) and gets
  measurably further into its own startup than it ever did before.

  It then hits the *actual* final wall: `SysUpMain Failed s32Ret: 36`.
  `strings` confirms `SysUpMain` opens `/dev/mem` and `mmap()`s it
  directly to talk to the Hi3559's MPP (media processing) hardware
  registers - not an `ioctl()` on a device file, but raw physical
  memory access expecting a real SoC's register layout on the other
  end. That's not fakeable with another shim; it would mean modeling
  the actual behavior of Hisilicon's proprietary MPP register set, a
  different scale of project (closer to a chip simulator than a
  syscall interceptor) and out of scope here. This is the genuine end
  of the userspace-emulation road for `main_app`.
