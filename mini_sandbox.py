#!/usr/bin/env python3
"""
mini-sandbox: Linux namespace を使ったミニマルコンテナランタイム

使い方:
  mini-sandbox --ro-bind / / -- ls /
  mini-sandbox --ro-bind / / --bind /tmp /tmp -- touch /tmp/test
  mini-sandbox --ro-bind / / --unshare-pid -- ps aux
  mini-sandbox --ro-bind / / --unshare-pid --unshare-net -- curl example.com
  mini-sandbox --rootfs ./rootfs --unshare-pid --unshare-net -- /bin/cat /etc/os-release
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


def setup_fs(ro_binds, binds, rootfs, unshare_pid):
    # ここでのマウント操作がホスト側へ伝播しないようにする
    do_mount("", "/", "", MS_PRIVATE | MS_REC)

    if rootfs:
        # runc 方式: pivot_root でルートごと rootfs に差し替える
        rootfs = os.path.realpath(rootfs)
        # 自分自身に bind mount してマウントポイントにする (pivot_root の要件)
        do_mount(rootfs, rootfs, "", MS_BIND)

        old_root = os.path.join(rootfs, ".old_root")
        os.makedirs(old_root, exist_ok=True)
        do_pivot_root(rootfs, old_root)  # 旧ルートは .old_root に退避される
        os.chdir("/")

        # /proc は旧ルートを切り離す前にマウントする必要がある
        os.makedirs("/proc", exist_ok=True)
        if unshare_pid:
            do_mount("proc", "/proc", "proc", 0)
        else:
            # 新しい PID namespace がないと fresh な procfs はマウントできない
            # (カーネルの制約)。同じ PID 空間のままなのでホストの /proc を見せる
            do_mount("/.old_root/proc", "/proc", "", MS_BIND | MS_REC)

        # 旧ルート (ホストの FS) を切り離して到達不能にする
        do_mount("", "/.old_root", "", MS_PRIVATE | MS_REC)
        libc.umount2("/.old_root".encode(), MNT_DETACH)
        os.rmdir("/.old_root")
    else:
        # bubblewrap 方式: ホストの FS を重ねたまま read-only のフィルターをかける
        for src, dest in ro_binds:
            do_mount(src, dest, "", MS_BIND | MS_REC)
            do_mount("", dest, "", MS_REMOUNT | MS_BIND | MS_REC | MS_RDONLY)  # bind mount は作成時にフラグを指定できないので、再マウントで read-only 化

        # 書き込み許可するパスだけ上から重ねて read-only を打ち消す
        for src, dest in binds:
            if not os.path.exists(src):
                continue
            do_mount(src, dest, "", MS_BIND | MS_REC)
            do_mount("", dest, "", MS_REMOUNT | MS_BIND | MS_REC)

        # 新しい PID namespace の中身を映すために /proc をマウントし直す
        if unshare_pid:
            do_mount("proc", "/proc", "proc", 0)


def run(command, ro_binds, binds, rootfs, unshare_pid, unshare_net):
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
        setup_fs(ro_binds, binds, rootfs, unshare_pid)
        os.execvp(command[0], command)

    # PID だけは別: プロセスの PID は生成時に決まるので、既存プロセスは
    # 新しい PID namespace に移れない。unshare 後に fork した子が最初の住人になる
    pid = os.fork()
    if pid == 0:
        # 子プロセス: 新しい PID namespace 内で PID 1
        try:
            setup_fs(ro_binds, binds, rootfs, unshare_pid)
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
    rootfs = None
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
        elif arg == "--rootfs":
            if i + 1 >= len(args):
                print("error: --rootfs requires PATH", file=sys.stderr)
                sys.exit(2)
            rootfs = args[i + 1]
            i += 2
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

    if not ro_binds and not rootfs:
        print("error: specify --ro-bind or --rootfs", file=sys.stderr)
        sys.exit(2)

    if ro_binds and rootfs:
        print("error: --ro-bind and --rootfs are mutually exclusive", file=sys.stderr)
        sys.exit(2)

    run(command, ro_binds, binds, rootfs, unshare_pid, unshare_net)


if __name__ == "__main__":
    main()
