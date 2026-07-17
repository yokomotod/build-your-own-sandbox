# build-your-own-sandbox

Claude Code / Codex のサンドボックスに使われている [bubblewrap](https://github.com/containers/bubblewrap) を Python で自作して、Linux namespace によるサンドボックス / コンテナの仕組みを理解するためのサンプルコードです。

解説記事: <!-- TODO: 公開後に記事URLを追記 -->

## 動作環境

- Linux（Ubuntu 24.04 で動作確認）
- Python 3.12 以上（`os.unshare` を使用）

### Ubuntu 24.04 以降での事前設定

Ubuntu 24.04 以降は AppArmor が非特権ユーザーの user namespace 作成をデフォルトで制限しているため、実行する Python バイナリに対してプロファイルの追加が必要です。

```bash
PYTHON_BIN=$(readlink -f $(which python3))
sudo tee /etc/apparmor.d/python3-userns <<EOF
abi <abi/4.0>,
include <tunables/global>

profile python3-userns "$PYTHON_BIN" flags=(unconfined) {
  userns,
  include if exists <local/python3-userns>
}
EOF
sudo apparmor_parser -r /etc/apparmor.d/python3-userns
```

この設定はこの Python バイナリから実行されるすべてのスクリプトに user namespace の作成を許可するものです。実験が終わったら削除してください。

```bash
sudo apparmor_parser -R /etc/apparmor.d/python3-userns
sudo rm /etc/apparmor.d/python3-userns
```

## 使い方

### bubblewrap 風サンドボックス

```bash
# ホストのFSを読み取り専用で見せる (/tmp だけ書き込み可)
python3 mini_sandbox.py --ro-bind / / --bind /tmp /tmp -- ls /

# + プロセス隔離
python3 mini_sandbox.py --ro-bind / / --unshare-pid -- ps aux

# + ネットワーク遮断
python3 mini_sandbox.py --ro-bind / / --unshare-pid --unshare-net -- curl https://example.com
```

### コンテナ風 (rootfs 差し替え)

```bash
# Alpine Linux の minirootfs を「イメージ」として使う
mkdir rootfs
curl -fsSL https://dl-cdn.alpinelinux.org/alpine/v3.21/releases/x86_64/alpine-minirootfs-3.21.4-x86_64.tar.gz \
  | tar xz -C rootfs

python3 mini_sandbox.py --rootfs ./rootfs --unshare-pid --unshare-net -- /bin/cat /etc/os-release
```

### 隔離状態の観測

```bash
python3 mini_sandbox.py --ro-bind / / --unshare-pid -- bash observe.sh
```

## 注意

学習用の最小実装です。本物の bubblewrap が行っている capabilities の制御、seccomp フィルタ、init プロセスとしてのシグナルハンドリングなどは省略しています。実用のサンドボックスとしては使わないでください。
