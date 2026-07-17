#!/usr/bin/env python3
"""
mini-sandbox: Linux namespace を使ったミニマルサンドボックス

使い方:
  mini-sandbox --ro-bind / / -- ls /
  mini-sandbox --ro-bind / / --bind /tmp /tmp -- touch /tmp/test
  mini-sandbox --ro-bind / / --unshare-pid -- ps aux
  mini-sandbox --ro-bind / / --unshare-pid --unshare-net -- curl example.com
  mini-sandbox --ro-bind ./rootfs / --unshare-pid -- /bin/cat /etc/os-release
"""
import ctypes
import ctypes.util
import os
import sys

# mount / pivot_root システムコールは Python の os モジュールにないので
# ctypes で libc を直接呼ぶ
libc = ctypes.CDLL(ctypes.util.find_library("c"), use_errno=True)

# <sys/mount.h> のマウントフラグ
MS_RDONLY = 0x1      # read-only でマウント
MS_REMOUNT = 0x20    # 既存マウントのフラグを変更
MS_BIND = 0x1000     # bind mount (既存のパスを別の場所に重ねてマウント)
MS_REC = 0x4000      # サブマウントにも再帰的に適用
MS_PRIVATE = 0x40000 # マウントイベントを他の namespace に伝播させない
MNT_DETACH = 2       # umount2 用: 使用中でも遅延切り離し


def do_mount(source, target, fstype, flags, data=""):
    ret = libc.mount(
        source.encode() if source else None,
        target.encode(),
        fstype.encode() if fstype else None,
        flags,
        data.encode() if data else None,
    )
    if ret != 0:
        errno = ctypes.get_errno()
        raise OSError(errno, f"mount({source}, {target}, flags={flags:#x}): {os.strerror(errno)}")


def do_pivot_root(new_root, put_old):
    ret = libc.pivot_root(new_root.encode(), put_old.encode())
    if ret != 0:
        errno = ctypes.get_errno()
        raise OSError(errno, f"pivot_root({new_root}, {put_old}): {os.strerror(errno)}")


def setup_uid_map(pid, uid, gid):
    # uid_map より先に setgroups を deny しないと書き込みが拒否される
    with open(f"/proc/{pid}/setgroups", "w") as f:
        f.write("deny\n")
    # 「namespace内のid ホストのid 範囲」を書き込むとユーザーが決まる
    with open(f"/proc/{pid}/uid_map", "w") as f:
        f.write(f"{uid} {uid} 1\n")
    with open(f"/proc/{pid}/gid_map", "w") as f:
        f.write(f"{gid} {gid} 1\n")


def setup_fs(ro_binds, binds, unshare_pid):
    # ここでのマウント操作がホスト側へ伝播しないようにする
    do_mount("", "/", "", MS_PRIVATE | MS_REC)

    # 空の tmpfs を newroot として作る (tmpfs 自体がマウントポイントなので pivot_root の要件を満たす)
    # (本家 bwrap は tmpfs の中に newroot サブディレクトリを切って pivot_root を2回行うが、簡略化)
    do_mount("tmpfs", "/tmp", "tmpfs", 0)

    # newroot の中に --ro-bind / --bind で指定されたパスをマウントしていく
    for src, dest in ro_binds:
        src = os.path.realpath(src)
        target = f"/tmp{dest}"
        os.makedirs(target, exist_ok=True)
        do_mount(src, target, "", MS_BIND | MS_REC)
        do_mount("", target, "", MS_REMOUNT | MS_BIND | MS_REC | MS_RDONLY)  # bind mount は作成時にフラグを指定できないので、再マウントで read-only 化

    for src, dest in binds:
        src = os.path.realpath(src)
        target = f"/tmp{dest}"
        os.makedirs(target, exist_ok=True)
        do_mount(src, target, "", MS_BIND | MS_REC)
        do_mount("", target, "", MS_REMOUNT | MS_BIND | MS_REC)

    # /proc をマウント
    proc_target = "/tmp/proc"
    os.makedirs(proc_target, exist_ok=True)
    if unshare_pid:
        do_mount("proc", proc_target, "proc", 0)
    else:
        do_mount("/proc", proc_target, "", MS_BIND | MS_REC)

    # pivot_root で newroot に切り替え
    # pivot_root(".", ".") は runc/LXC も使っているテクニックで、
    # put_old を newroot 内に別途用意する必要がない
    oldroot_fd = os.open("/", os.O_DIRECTORY | os.O_RDONLY)
    os.chdir("/tmp")
    do_pivot_root(".", ".")

    # 旧ルート (ホストの FS) を切り離して到達不能にする
    os.fchdir(oldroot_fd)
    do_mount("", ".", "", MS_PRIVATE | MS_REC)
    libc.umount2(".".encode(), MNT_DETACH)
    os.close(oldroot_fd)
    os.chdir("/")


def run(command, ro_binds, binds, unshare_pid, unshare_net):
    # unshare 後は uid_map 設定まで getuid() が nobody になるので先に保存
    host_uid = os.getuid()
    host_gid = os.getgid()

    # user namespace が他の namespace を非特権で作るための前提
    os.unshare(os.CLONE_NEWUSER)
    setup_uid_map(os.getpid(), host_uid, host_gid)

    ns_flags = os.CLONE_NEWNS
    if unshare_pid:
        ns_flags |= os.CLONE_NEWPID
    if unshare_net:
        ns_flags |= os.CLONE_NEWNET
    os.unshare(ns_flags)

    if not unshare_pid:
        # mount / network / user は unshare した時点で自分が移っているので
        # そのまま exec すれば namespace への所属は引き継がれる
        setup_fs(ro_binds, binds, unshare_pid)
        os.execvp(command[0], command)

    # PID だけは別: プロセスの PID は生成時に決まるので、既存プロセスは
    # 新しい PID namespace に移れない。unshare 後に fork した子が最初の住人になる
    pid = os.fork()
    if pid == 0:
        # 子プロセス: 新しい PID namespace 内で PID 1
        try:
            setup_fs(ro_binds, binds, unshare_pid)
            os.execvp(command[0], command)
        except Exception as e:
            print(f"mini-sandbox: {e}", file=sys.stderr)
            sys.exit(1)
    else:
        # 親プロセス: 子の終了を待って exit code を引き継ぐ
        _, status = os.waitpid(pid, 0)
        sys.exit(os.waitstatus_to_exitcode(status))


def usage():
    print((__doc__ or "").strip(), file=sys.stderr)
    sys.exit(2)


def main():
    args = sys.argv[1:]
    if not args:
        usage()

    ro_binds = []
    binds = []
    unshare_pid = False
    unshare_net = False
    command = []

    i = 0
    while i < len(args):
        arg = args[i]
        if arg == "--ro-bind":
            if i + 2 >= len(args):
                print("error: --ro-bind requires SRC DEST", file=sys.stderr)
                sys.exit(2)
            ro_binds.append((args[i + 1], args[i + 2]))
            i += 3
        elif arg == "--bind":
            if i + 2 >= len(args):
                print("error: --bind requires SRC DEST", file=sys.stderr)
                sys.exit(2)
            binds.append((args[i + 1], args[i + 2]))
            i += 3
        elif arg == "--unshare-pid":
            unshare_pid = True
            i += 1
        elif arg == "--unshare-net":
            unshare_net = True
            i += 1
        elif arg == "--":
            command = args[i + 1:]
            break
        elif arg == "--help" or arg == "-h":
            usage()
        else:
            command = args[i:]
            break

    if not command:
        print("error: no command specified", file=sys.stderr)
        sys.exit(2)

    if not ro_binds:
        print("error: at least one --ro-bind is required", file=sys.stderr)
        sys.exit(2)

    run(command, ro_binds, binds, unshare_pid, unshare_net)


if __name__ == "__main__":
    main()
