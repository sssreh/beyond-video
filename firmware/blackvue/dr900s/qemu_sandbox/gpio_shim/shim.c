/*
 * gpio_shim.c - LD_PRELOAD shim that fakes success for ioctl() calls on the
 * DR900S-2CH firmware's /dev/hi_gpio device node under qemu-arm-static
 * emulation.
 *
 * Why this exists: main_app opens /dev/hi_gpio (a fake char device we create
 * with `mknod`) fine, but the next call - HI_HAL_Gpio_GetBitVal's ioctl() -
 * fails, because the fake device node isn't backed by a real GPIO driver
 * that understands the ioctl request code main_app sends. This shim
 * intercepts every ioctl() call the emulated binary makes; if the real
 * ioctl() fails AND the fd points at a path containing "hi_gpio" (checked
 * via /proc/self/fd/<fd>), it fakes success (return 0) instead of letting
 * the real error through. Every other ioctl (on every other fd) passes
 * through to the real syscall completely unmodified.
 *
 * Deliberately freestanding / zero libc dependency: it's built with a
 * modern hard-float ARM cross-compiler (arm-none-linux-gnueabihf), but the
 * firmware's own libc is an old eglibc 2.20 soft-float build. Rather than
 * fight glibc symbol-version and float-ABI compatibility between the two,
 * this shim never calls into ANY libc function (no dlsym, no snprintf, no
 * strstr) - it talks to the kernel directly via raw ARM EABI syscalls
 * (svc #0, syscall number in r7, args in r0-r3). That sidesteps the whole
 * problem: there is nothing here for the dynamic linker to resolve against
 * the target's libc.so.6 at all, so it doesn't matter which toolchain or
 * float ABI built it.
 *
 * Build: see build.sh in this directory.
 * Use: see ../run_main_app.sh, which sets LD_PRELOAD to this .so (placed
 * inside the sysroot) before invoking qemu-arm-static on main_app.
 */

#define NR_read     3
#define NR_write    4
#define NR_open     5
#define NR_close    6
#define NR_ioctl    54
#define NR_readlink 85

static long raw_syscall3(long n, long a1, long a2, long a3) {
    register long r7 __asm__("r7") = n;
    register long r0 __asm__("r0") = a1;
    register long r1 __asm__("r1") = a2;
    register long r2 __asm__("r2") = a3;
    __asm__ volatile (
        "svc #0\n"
        : "+r"(r0)
        : "r"(r7), "r"(r1), "r"(r2)
        : "memory"
    );
    return r0;
}

static long raw_ioctl(int fd, unsigned long request, void *argp) {
    return raw_syscall3(NR_ioctl, fd, (long)request, (long)argp);
}

static long raw_readlink(const char *path, char *buf, unsigned long bufsiz) {
    return raw_syscall3(NR_readlink, (long)path, (long)buf, (long)bufsiz);
}

static long raw_write(int fd, const char *buf, unsigned long count) {
    return raw_syscall3(NR_write, fd, (long)buf, (long)count);
}

/* minimal freestanding helpers - no libc allowed (see file header) */

static unsigned long str_len(const char *s) {
    unsigned long n = 0;
    while (s[n]) n++;
    return n;
}

/* writes "/proc/self/fd/<fd>" into buf (assumed >= 32 bytes); returns length */
static int build_fd_path(int fd, char *buf) {
    const char *prefix = "/proc/self/fd/";
    int plen = (int)str_len(prefix);
    for (int i = 0; i < plen; i++) buf[i] = prefix[i];

    char digits[12];
    int ndigits = 0;
    unsigned int v = (fd < 0) ? 0 : (unsigned int)fd;
    if (v == 0) {
        digits[ndigits++] = '0';
    } else {
        while (v > 0) {
            digits[ndigits++] = (char)('0' + (v % 10));
            v /= 10;
        }
    }
    int pos = plen;
    for (int i = ndigits - 1; i >= 0; i--) buf[pos++] = digits[i];
    buf[pos] = '\0';
    return pos;
}

/* true if `needle` occurs anywhere in the first `hay_len` bytes of `hay` */
static int contains(const char *hay, int hay_len, const char *needle) {
    int nlen = (int)str_len(needle);
    if (nlen == 0 || hay_len < nlen) return 0;
    for (int i = 0; i <= hay_len - nlen; i++) {
        int j = 0;
        for (; j < nlen; j++) {
            if (hay[i + j] != needle[j]) break;
        }
        if (j == nlen) return 1;
    }
    return 0;
}

static void debug_log(const char *msg) {
    raw_write(2, msg, str_len(msg));
}

/*
 * The actual interposed symbol. Signature matches libc's ioctl() closely
 * enough for how main_app calls it (fd, request, single pointer/int arg).
 */
int ioctl(int fd, unsigned long request, void *argp) {
    long ret = raw_ioctl(fd, request, argp);
    if (ret < 0) {
        char linkpath[32];
        char target[128];
        build_fd_path(fd, linkpath);
        long n = raw_readlink(linkpath, target, sizeof(target) - 1);
        if (n > 0 && contains(target, (int)n, "hi_gpio")) {
            debug_log("[gpio-shim] faking ioctl() success on /dev/hi_gpio\n");
            return 0;
        }
    }
    return (int)ret;
}
