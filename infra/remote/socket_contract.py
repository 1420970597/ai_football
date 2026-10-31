"""Ephemeral Docker-only Unix socket ownership and reconnect acceptance."""
import json
import os
from pathlib import Path
import socket
import tempfile
import time
from .socket_guard import StaleSocketGuard, grant


def allowed(path, uid, gid):
    child=os.fork()
    if child==0:
        try:
            os.setgroups([]);os.setgid(gid);os.setuid(uid)
            with socket.socket(socket.AF_UNIX,socket.SOCK_STREAM) as client:
                client.settimeout(1);client.connect(str(path))
            os._exit(0)
        except PermissionError:os._exit(10)
        except Exception:os._exit(20)
    _,status=os.waitpid(child,0)
    return os.waitstatus_to_exitcode(status)


def main():
    if os.geteuid()!=0 or not Path('/.dockerenv').exists():
        raise ValueError('isolated_root_container_required')
    with tempfile.TemporaryDirectory(prefix='remote-contract-') as temporary:
        root=Path(temporary);root.chmod(0o755)
        directory=root/'remote';directory.mkdir();os.chown(directory,0,10005);directory.chmod(0o2750)
        target=directory/'api.sock'
        server=socket.socket(socket.AF_UNIX,socket.SOCK_STREAM);server.bind(str(target));server.listen(8)
        os.chown(target,0,10005)
        assert grant(directory)
        assert allowed(target,10006,10005)==0
        assert allowed(target,10007,10007)==10
        guard=StaleSocketGuard()
        assert guard.observe(directory)=='listening'
        server.close()
        assert guard.observe(directory,now=1,wall_now=time.time()+60)=='refused'
        assert guard.observe(directory,now=3,wall_now=time.time()+62)=='stale_removed'
        server=socket.socket(socket.AF_UNIX,socket.SOCK_STREAM);server.bind(str(target));server.listen(8)
        os.chown(target,0,10005)
        assert grant(directory) and allowed(target,10006,10005)==0
        assert guard.observe(directory)=='listening'
        server.close();target.unlink()
        target.write_text('synthetic')
        try:grant(directory)
        except ValueError:pass
        else:raise AssertionError('regular_file_accepted')
    print(json.dumps({'private_group_connect':True,'other_uid_denied':True,'closed_socket_recovered':True,
                      'live_socket_preserved':True,'regular_file_rejected':True,'production_touched':False}))


if __name__=='__main__':main()
