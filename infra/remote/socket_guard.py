"""Grant one service group access to the exact SSH socket, never to regular files."""
from __future__ import annotations
import os
import errno
from pathlib import Path
import stat
import socket
import time

DIRECTORY = Path('/run/remote')
STALE_SECONDS = 30


def grant(directory: Path = DIRECTORY) -> bool:
    parent = directory.lstat()
    if not stat.S_ISDIR(parent.st_mode) or parent.st_uid != 0 or parent.st_gid != 10005 or stat.S_IMODE(parent.st_mode) != 0o2750:
        raise ValueError('remote_socket_directory_invalid')
    try:
        info = (directory / 'api.sock').lstat()
    except FileNotFoundError:
        return False
    if not stat.S_ISSOCK(info.st_mode) or info.st_uid != 0 or info.st_gid != 10005:
        raise ValueError('remote_socket_owner_invalid')
    if stat.S_IMODE(info.st_mode) != 0o660:
        # chmod does not follow a swapped symlink: reject inode/type change first,
        # and the root-owned directory is unwritable by the service group.
        os.chmod(directory / 'api.sock', 0o660, follow_symlinks=False)
    return True


class StaleSocketGuard:
    """Remove only an unchanged refused socket after two observations and age.

    OpenSSH server StreamLocalBindUnlink governs remote -R Unix sockets; the
    client's option cannot repair a server configured with it disabled. This
    project-local guard never removes a listener, regular file or symlink.
    """
    def __init__(self):
        self.refused = None

    @staticmethod
    def associated(target):
        # Compose gives this tiny guard the host network namespace, so its
        # kernel Unix socket table also sees the host sshd listener. No host
        # /proc mount or process access is required. A bound but not listening
        # socket is still associated and must not be removed.
        name = '/root/ai_football/.runtime/remote/api.sock' if target == DIRECTORY / 'api.sock' else str(target)
        for line in Path('/proc/net/unix').read_text().splitlines()[1:]:
            fields = line.split(maxsplit=7)
            if len(fields) == 8 and fields[7] == name:
                return True
        return False

    def observe(self, directory=DIRECTORY, *, now=None, wall_now=None):
        if not grant(directory):
            self.refused = None
            return 'absent'
        target = directory / 'api.sock'
        info = target.lstat()
        identity = (info.st_dev, info.st_ino, info.st_ctime_ns)
        try:
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as probe:
                probe.settimeout(.25)
                probe.connect(str(target))
            self.refused = None
            return 'listening'
        except OSError as exc:
            if exc.errno != errno.ECONNREFUSED:
                self.refused = None
                return 'unverified'
        if self.associated(target):
            self.refused = None
            return 'associated'
        moment = time.monotonic() if now is None else now
        age = (time.time() if wall_now is None else wall_now) - info.st_ctime
        if self.refused is None or self.refused[0] != identity:
            self.refused = (identity, moment)
            return 'refused'
        if age < STALE_SECONDS or moment - self.refused[1] < 1:
            return 'refused'
        # The parent is root-owned and not group-writable; only the SSH daemon
        # or this guard can replace the entry. Recheck inode/type/ownership.
        current = target.lstat()
        if (current.st_dev, current.st_ino, current.st_ctime_ns) != identity:
            self.refused = None
            return 'changed'
        if not stat.S_ISSOCK(current.st_mode) or (current.st_uid, current.st_gid) != (0, 10005):
            raise ValueError('remote_socket_owner_invalid')
        if self.associated(target):
            self.refused = None
            return 'associated'
        target.unlink()
        self.refused = None
        return 'stale_removed'


if __name__ == '__main__':
    guard = StaleSocketGuard()
    while True:
        try:
            guard.observe()
        except (OSError, ValueError):
            raise SystemExit('remote_socket_permission_rejected')
        time.sleep(.25)
