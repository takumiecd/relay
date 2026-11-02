# Relay CLI：起動中のセッションを端末から操作する

ブラウザと同じHTTP APIへ接続する、標準ライブラリだけのクライアント。
CLIで別のシミュレータを起動するのではなく、**既に起動したRelayの状態と操作権を共有**する。
この段階ではサーバー／既存APIを変更しない。ブラウザの再読み込みや、稼働中サービスの再起動は不要。

## 起動方法

リポジトリのルートから、Python 3.10以降で次を実行できる。CLI側にTorch・ROS 2・Genesis等は不要。

```bash
python -m relay --url http://127.0.0.1:8883 status
```

既存の開発環境へプロジェクトをインストールすると、`relay`コマンドも登録される。
CLIだけの専用venvを作る場合は次の方法も使える。`--no-deps`は**CLI専用環境**のためで、
同じvenvでシミュレータ／CaP-X本体が動くという意味ではない。プロジェクトmetadataの対応Pythonは3.10〜3.12。

```bash
uv venv --python 3.12 .local/relay-cli
uv pip install --python .local/relay-cli/bin/python --no-deps -e .
source .local/relay-cli/bin/activate
relay --url http://127.0.0.1:8883 status
```

以下はcheckpoint対応のLIBERO／Genesisセッションへ接続する例。
ポートは、そのセッションの起動時に表示されたURLへ合わせる。

```bash
export RELAY_URL=http://127.0.0.1:8876
relay status
relay control human
relay stop
relay history list
relay history seek --branch 0001 --step 60
relay history adopt --branch 0002 --step 100
```

`--url`が環境変数`RELAY_URL`より優先。どちらも省略すると`http://127.0.0.1:8876`へ接続する。
現在のAPIに合わせ、接続先は`http://127.0.0.1:PORT`または`http://localhost:PORT`。
CLIはページと同じ認証tokenをメモリ上で取得し、既存の`X-CapX-Token`を使う。認証は無効化しない。
既存のtokenを明示する必要がある場合だけ、環境変数`RELAY_TOKEN`を使える。
VLA worker向けの`connection.json`は別サービスの接続情報なので、このCLIへ渡さない。
tokenを引数やログへ出す機能は設けない。環境変数のHTTP proxyやHTTP redirectも使用しない。

## コマンド

| コマンド | 動作 |
|---|---|
| `status` | mode・epoch・backend・VLA／LLMの接続状態・指令数・履歴・採用経路・エラーを表示 |
| `control human` | 手動モードへ切替。このCLIのclient IDへ操作権を渡す |
| `control vla [--instruction "指示"]` | 準備済みのVLAへ切替。省略時はサーバー側の既定指示 |
| `control llm --instruction "指示"` | 準備済みのLLMへコード生成・実行を要求。指示は必須 |
| `stop` | Escと同じSTOPPEDへの切替。`control stopped`も同じ要求 |
| `history list` | 経路ID、head、親経路、分岐stepを表示。`*`は現在の経路 |
| `history seek --step N [--branch ID]` | 指定したcheckpointへ戻して停止。branch省略時は現在の経路 |
| `history adopt [--step N] [--branch ID]` | 指定経路を採用。省略時は現在のbranch／cursor |

`control human`は端末でWASDを読み続ける機能ではない。キーボードの移動入力は引き続きブラウザから送る。
CLIで切り替えた後、ブラウザでHまたは手動中のSpaceを押し直すと、その画面へ操作権を引き継げる。
切替前のclient／epochの入力は拒否される。CLIが終了しても選択したmodeは維持される。

巻き戻した地点で次の動作を始めると、既存処理が新しい分岐を作る。元の未来は保持する。
採用は経路の選択・書き出しで、学習投入ではない。`training_admitted=false`を維持する。
このCLIの追加でbackendの能力は増えない。ROS参照版のようにcheckpointがない接続先では、
状態確認・切替・停止を使えるが、巻き戻し・経路採用は対応外として拒否する。

## 受理と完了、競合

mode要求には、CLIが読んだ`expected_epoch`、client ID、sequenceを付ける。
別の操作と競合して`accepted=false`になった要求は、CLIから再送しない。
途中でセッションが変わった場合や、別clientによる採用結果を見た場合も成功とは表示しない。

`control`は切替が反映された状態を待つ。LLMの生成・コード実行やVLAの動作が終わるまで待つ意味ではない。
`history seek/adopt`はサーバーが要求を受け付けた後、処理中フラグだけでなく、
指定branch／stepまたは自分の採用結果を確認してから成功を返す。

`stop`は状態のGETを待たず、先に停止要求を送る。受理後、最大1秒だけ状態表示の更新を確認する。
`state_confirmation=confirmed`なら新しいepochのSTOPPEDを取得済み。
`pending/unavailable`は停止要求の受理は確認済みだが、状態表示を確認できなかったことを示す。
後から別の開始操作が入っていた場合は`superseded`と競合を報告する。
いずれもcontroller側の物理的な停止完了を証明するフィールドではない。

POSTの応答が失われた場合も再送しない。受理済みの可能性があるため`status`で確認する。
Ctrl+CはCLIの待機を中断し、既に受理された要求を自動キャンセルしない。

```bash
relay --wait-timeout 60 history adopt --branch 0002 --step 100
relay status --json
relay history list --json
```

共通オプションはコマンドより前へ置く。`--json`だけは末尾にも指定できる。
`--timeout`はHTTP要求の期限（既定5秒）、`--wait-timeout`は受理後の状態更新・履歴処理の待機期限（既定30秒）。
停止後の状態確認は最大1秒で、`--wait-timeout`が1秒未満ならそちらを使う。

JSONではstatusはAPIの状態snapshot、history listはtimelineを返す。
変更要求は`action/accepted/completed/client_id/sequence/response/state`のreceiptを返す。
`completed`はその管理操作の完了であり、LLMのプログラム完了やタスク成功とは異なる。
エラーは`error.code/message`、受理後のエラーには既知のreceiptを付ける。

| 終了コード | 意味 |
|---|---|
| 0 | 状態取得・要求の受理／対象の履歴処理完了。stopの状態確認有無はreceiptで区別 |
| 1 | 接続・API・未対応機能・実行エラー |
| 2 | CLI構文・引数型の誤り |
| 3 | 操作状態・セッション・採用結果の競合 |
| 4 | 受理後の完了待ち期限切れ。処理は継続している可能性あり |
| 130 | Ctrl+Cで待機を中断 |

## Pythonからも同じ窓口を使う

```python
from relay.client import RelayClient

client = RelayClient("http://127.0.0.1:8876")
state = client.status()
receipt = client.seek(60, branch="0001")
receipt = client.adopt(branch="0002", step=100)
client.stop()
```

一つのclientでは操作を順番に呼ぶ。ブラウザ・CLI・Notebookの物理実行先は同じサーバーのowner thread。
`relay/client.py`がHTTP、認証、epoch、完了確認を担当し、`relay/cli.py`は引数と表示を担当する。
Runtime自体のCaP-Xからの独立は、このCLIの次の変更として扱う。
