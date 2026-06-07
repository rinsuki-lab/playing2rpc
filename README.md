# playing2rpc

Discordクライアントはローカルで起動しているゲームを実行ファイル名やSteamの App ID で判定し、プレイ中ゲームとして表示しますが、Discordクライアントとゲームを実行しているマシンが同じでない場合はこの機能の恩恵を受けることができません。

そこで、playing2rpc は Discord クライアントと同じ判定を自前で行い、Discord の App ID を Rich Presence として送信することで、Rich Presence 非対応のゲームを (Discordクライアントをゲームマシンで動かすことなしに) Discord クライアント上でプレイ中表示にすることを可能とします。

## 前提環境

* Linux x86_64
  * FEX-emu 上で Wine を動かしている場合は Wine 判定がうまく動かず Windows ゲームの判定が動かないかも?
* デスクトップ環境が KDE である
  * KDE のウィンドウ作成イベントをトリガーとして判定しているため
  * Wayland 環境でのみテスト済み、X11環境で動くかは未検証
* 新しめの Python 3.x
* `pypresence`, `dbus-next`
  * Arch Linux なら `sudo pacman -S python-pypresence python-dbus-next`

## 使い方

* Discord クライアントと playing2rpc を動かすマシンが別の場合、事前に `discord-ipc-X` を別の手段で転送します
  * e.g. Discord クライアントが macOS で動いている場合、macOS マシン上で `ssh -R /run/user/ゲームマシン上のUID/discord-ipc-0:$TMPDIR/discord-ipc-0 ゲームマシン` などで転送できます
* detectable.json を `curl -o detectable.json https://discord.com/api/v10/applications/detectable` でダウンロードします
* playing2rpc の main.py を起動します
* ゲームを起動します
* Discord クライアント側でゲーム中になってたらOK

