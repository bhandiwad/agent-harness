"""Linux Landlock defense in depth. Selected enforcement never silently degrades."""

import ctypes
import os
import platform
import sys


def enforce():
    if sys.platform != "linux" or platform.machine() not in {"x86_64", "aarch64"}:
        raise RuntimeError("Landlock needs a supported Linux architecture.")
    libc = ctypes.CDLL(None, use_errno=True)

    def checked(number, *args):
        result = libc.syscall(ctypes.c_long(number), *args)
        if result < 0:
            raise OSError(ctypes.get_errno(), "Landlock operation failed")
        return result

    abi = checked(444, ctypes.c_void_p(), ctypes.c_size_t(0), ctypes.c_uint(1))
    if abi < 3:
        raise RuntimeError("Landlock ABI 3 or newer is required (truncate protection).")

    class Rules(ctypes.Structure):
        _fields_ = [("fs", ctypes.c_uint64), ("net", ctypes.c_uint64), ("scope", ctypes.c_uint64)]

    class PathRule(ctypes.Structure):
        _pack_ = 1
        _fields_ = [("allowed", ctypes.c_uint64), ("parent", ctypes.c_int)]

    fs = (1 << 15) - 1
    rules = Rules(fs, 3 if abi >= 4 else 0, 3 if abi >= 6 else 0)
    size = 24 if abi >= 6 else (16 if abi >= 4 else 8)
    fd = checked(444, ctypes.byref(rules), ctypes.c_size_t(size), ctypes.c_uint(0))
    read = (1 << 0) | (1 << 2) | (1 << 3)
    write = fs & ~((1 << 6) | (1 << 11))  # No device-node creation.
    try:
        for path, access in [
            ("/usr", read),
            ("/bin", read),
            ("/lib", read),
            ("/lib64", read),
            ("/opt", read),
            ("/etc", read),
            ("/proc", read),
            ("/dev", read | 2),
            ("/workspace", write),
            ("/tmp", write),
        ]:
            if not os.path.exists(path):
                continue
            parent = os.open(path, os.O_PATH | os.O_CLOEXEC)
            try:
                rule = PathRule(access, parent)
                checked(
                    445, ctypes.c_int(fd), ctypes.c_int(1), ctypes.byref(rule), ctypes.c_uint(0)
                )
            finally:
                os.close(parent)
        if libc.prctl(38, 1, 0, 0, 0) != 0:
            raise OSError(ctypes.get_errno(), "no_new_privs failed")
        checked(446, ctypes.c_int(fd), ctypes.c_uint(0))
    finally:
        os.close(fd)
    return abi


if __name__ == "__main__":
    try:
        enforce()
        if len(sys.argv) > 1:
            os.execvpe(
                sys.argv[1],
                sys.argv[1:],
                {
                    key: os.environ[key]
                    for key in (
                        "PATH",
                        "HOME",
                        "TMPDIR",
                        "LANG",
                        "PYTHONDONTWRITEBYTECODE",
                        "PLAYWRIGHT_BROWSERS_PATH",
                    )
                    if key in os.environ
                },
            )
    except Exception:
        print("Required Landlock enforcement is unavailable; execution refused.", file=sys.stderr)
        raise SystemExit(125) from None
