# uninstallable

`curl … | sh` の最後を `uninstallable` に置き換えて、miseでアンインストールできるようにします。

## Usage

```sh
curl -fsSL https://bun.sh/install | uninstallable
mise uninstall bun
```

アプリ名・保存先を変更する環境変数・実行シェルを自動検出します。Bun・Deno・uvで動作を確認しています。

保存先を判定できない場合は、通常インストールを続行するか尋ねます。続行した場合はmise管理の対象外です。対話端末がない場合は中止します。

```sh
# インストールせず、検出結果を確認
curl -fsSL https://bun.sh/install | uninstallable --dry-run

# オプション一覧（名前・保存先変数・実行シェルなどの手動指定）
uninstallable --help
```

インストーラーへの引数は `uninstallable -- 引数` で渡せます。

インストール後はmiseのグローバル設定で有効になります。`mise uninstall foo` は本体を削除しますが、設定の指定と再インストール用プラグインは残ります。設定の指定も取り除く場合は `mise unuse --global foo` を使ってください。

## Install

必要なものは、macOSまたはLinux、Bash 3.2以降、jq 1.6以降、PATHにあるmiseです。通常のUnixコマンドと、配布元のインストーラーが必要とするコマンドも使用します。

このリポジトリを取得したディレクトリで実行します。

```sh
mkdir -p "$HOME/.local/bin"
install -m 755 uninstallable.sh "$HOME/.local/bin/uninstallable"
```

`$HOME/.local/bin` をPATHに含めてください。コピーせず試す場合は `./uninstallable.sh` を使えます。

## Motivation

`curl … | sh` は手軽ですが、削除方法や保存先がアプリごとに異なります。

配布元のインストーラーを利用しつつ、保存先をmiseの管理下に集めることで、削除を `mise uninstall foo` に揃えるために作りました。

## Goles / Non-Goles

**Goles**

- 配布元のインストールコマンドは、末尾の `sh` や `bash` を置き換えるだけで使う。
- 保存先の変更に対応するインストーラーを自動検出し、miseで管理する。
- 単一のシェルスクリプトとjqで動かす。

**Non-Goles**

- 任意のスクリプトの完全な解析や、安全な隔離実行。信頼できるインストーラーが前提です。
- miseの管理外に書き込まれた設定・キャッシュ・ユーザーデータの削除。
- アプリの自動更新や複数バージョンの共存。更新は削除後に再インストールします。
- パイプ元のcurlが正常終了したことの保証。必要な場合は、ダウンロード成功を確認してから保存したスクリプトを渡してください。

## License

[MIT](LICENSE)。取得するインストーラーとアプリ本体には、それぞれのライセンスが適用されます。
