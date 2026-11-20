"""Apply Linux resource limits before exec, without preexec_fn in a threaded server."""
import os
import resource
import sys


def apply_limit(kind, maximum):
    current, hard = resource.getrlimit(kind)
    if current == resource.RLIM_INFINITY or current > maximum:
        resource.setrlimit(kind, (maximum, hard))


if __name__ == "__main__":
    apply_limit(resource.RLIMIT_AS, 768 * 1024 * 1024)
    apply_limit(resource.RLIMIT_FSIZE, 105_000_000)
    apply_limit(resource.RLIMIT_CORE, 0)
    os.execv(sys.argv[1], sys.argv[1:])
