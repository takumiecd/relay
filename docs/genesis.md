# Genesis：キーボード操作・記録・巻き戻し

Genesis 1.4.5のPandaアーム・床・箱を、Relayの共通backend経由で動かす。
キーボード操作、2カメラ表示、実行した指令・観測の保存、新しい試行に対応する。
`feat/genesis-history`ではCPUの同じ稼働中scene/reset内での巻き戻し・分岐・経路採用も追加した。
LIBEROのタスクではなく、成功判定のない操作用sandboxである。

## 専用Python環境

リポジトリ直下で実行する。LIBERO用の`.local/openvla-sim`と分ける。

```bash
uv venv --python 3.12 .local/genesis-sim
uv pip install --no-config --python .local/genesis-sim/bin/python \
  -r relay/backends/requirements-genesis.txt
```

検証環境はmacOS Apple Silicon、Python 3.12、Genesis 1.4.5、CPU物理計算、offscreen描画。
NumPy 1.26.4／SciPy 1.14.1を明示し、LIBEROのMuJoCo／Torch依存を変更しない。
`--no-config`はCaP-X用のuv依存overrideを引き継がないために指定する。
Genesisの描画・画像依存も専用requirementsの版にそろえる。
CUDA／Metalでの動作確認は今回の対象外。
checkpointはGenesis 1.4.5・CPUで検証した構成に限定する。
他のdevice／SDKで従来の手動操作だけを使う場合は、設定に`"checkpoints": false`を指定する。

## 起動

同梱の[configs/relay/genesis-panda.json](../configs/relay/genesis-panda.json)を使う。

```bash
.local/genesis-sim/bin/python -m capx.extensions.hybrid.server \
  --backend genesis --backend-config configs/relay/genesis-panda.json \
  --manual-only --port 8877 --output output/genesis-manual-01
```

`http://127.0.0.1:8877/`を開く。出力先は未使用のフォルダ名にする。
初回はシーン・描画・逆運動学のコンパイルを待ち、`console_ready`が出てから操作する。
`--manual-only`はVLA／LLMへの自動接続を作らない。VLA／LLMボタンは未接続で無効になる。
記録は起動時から準備し、Spaceを押して物理stepを実行すると追記する。

## キーボード

- Hで手動へ。Spaceを押している間だけ物理stepを進める。
- WASD：WORLDのXY移動。W/Sが±X、A/Dが±Y。R/Fが上下±Z。
- Q/E：WORLDのZ軸回転。Z/X：WORLDのX軸回転。
- Cで閉じる、Vで開く。開閉指令を選んだあとSpaceだけでも動く。
- 速度スライダーで移動／回転量を変更し、Shiftでさらに1/5にする。
- Spaceを離す、入力が期限切れになる、またはEscを押すとholdする。
- 「新しい試行」で現在の記録を保存・exportし、同じsandboxを初期状態へ戻す。

手首カメラを初期主表示とし、全体カメラと主表示・配置を切り替えられる。
操作中は最後に実行した手先目標から入力を積み上げ、Spaceのみではその目標を維持する。
PD制御の追従中はSpaceを押し続けることで目標へ収束する。Spaceを離すと物理時間自体を止める。

## 巻き戻し・分岐・採用経路

同梱のCPU設定では`checkpoints: true`で起動する。

1. H→Space＋WASD/RF等で操作し、Escで止める。
2. カメラ下のシークバー・step番号・分岐図の点で、記録済みのstepを選ぶ。
   その物理状態へ戻り、停止する。進んでいた元の未来も残る。
3. Hまたは「ここから手動再開」で操作を続ける。途中から新しい操作を実行すると別の分岐になる。
4. 経路の選択欄や分岐図で、元の経路・別の分岐へ行き来できる。
5. 「この経路を採用」で、選んだ経路の末尾までを固定して書き出す。
   以後の操作で経路を延長しても、この採用結果の終点は変わらない。

`history/manifest.json`に分岐・現在位置・採用結果、`history/checkpoints/*.npz`に状態を保存する。
`branches/0002/trajectory/`等には分岐後の新規区間だけを記録し、親の先頭区間を参照する。
元のraw記録・checkpoint・採用した経路は上書きしない。
「新しい試行」は全経路を保存してresetし、別の履歴を開始する。

checkpointにはGenesis SDKの制御入力・接触／拘束計算の状態・warm start・物理パラメータ・導出済み姿勢を含め、
Relay側の関節制御目標・手先目標・開閉・乱数状態も保存する。
配列の整数／真偽値／floatのdtypeを維持し、数値とJSONだけで保存する。pickleは使用しない。
復元時はSDK版・sceneのdigest・model/resetの識別子と各配列の形／dtypeを検証し、
owner threadで復元した後、epochを更新して古い推論・キー入力を無効にする。

これは**同じプロセス・同じscene／reset内**の復元である。
アプリ再起動後の保存セッション再開、reset前の別試行への復元、別SDK／GPUへの移送には未対応。
`state_replay`は引き続きfalseとし、保存した指令列から厳密に再構成できるとは扱わない。

## 指令が関節へ届く経路

```text
キーボードの6軸入力と開閉
  → GenesisBackend.teleop_action：WORLDの絶対手先目標を構築
  → RelayEnvironment.apply_native：権限・epoch・停止・記録の確認
  → Genesis inverse_kinematics：手先目標から関節目標へ
  → control_dofs_position：関節PD制御
  → scene.step：実際の物理シミュレーション
```

native actionは8次元：`[x_m, y_m, z_m, qw, qx, qy, qz, gripper]`。
action契約は`genesis.panda.eef_world.absolute.xyz_wxyz.grip.8.v1`。
LIBEROの7次元相対OSC指令とは異なる。OpenVLAをこのまま接続した結果とは扱わない。
今回VLA／LLMの実モデル推論は接続・検証していない。

通常操作で関節状態を直接書き換えず、IK→PD→物理stepを使う。
`set_dofs_position`を使うのはresetの初期配置だけ。
20Hz制御の設定では、1操作につきdt=0.01秒の物理stepを5回実行する。
壁時計の速度は描画・計算負荷によって変わるが、記録にはシミュレーション時刻も残す。
手先目標の範囲・移動上限・IK誤差を確認し、解けない指令は物理step前に拒否する。

## 保存されるもの

- `trajectory/episode.json`：環境・制御器・版・周期・指令契約・記録の出所。
- `trajectory/steps.jsonl`：実行した8次元手先目標、前後の観測参照、操作主体。
  `backend_diagnostics`にはIKで得た9関節の制御目標、IK誤差、物理step数も保存する。
- `trajectory/observations/`：手首／全体RGB画像、実測関節・手先・箱の状態、時刻、数値状態ベクトル。
- `events.jsonl`：モード切替・停止など。
- `history/`・`branches/`：checkpoint対応時の分岐・復元点・採用結果と修正区間。
- 新しい試行は`trials/0002/trajectory/`等に分け、前の記録を上書きしない。

操作を自動テストする場合は`--test-operator`を付けて`TEST_OPERATOR`と記録する。
利用者が操作する通常起動では付けずに`HUMAN`として保存する。
品質確認・学習投入は別工程で、保存しただけでは`training_admitted=true`にしない。

診断用の32次元状態ベクトルだけを完全なGenesis状態として復元しない。
復元には上記のSDK checkpointを使う。`checkpoints: false`ならタイムラインを表示せず、通常の記録は続ける。
機械的な`replay_actions()`コードの自動生成は引き続き行わない。

## 実装と確認

[手動操作の検証結果](verification/genesis-keyboard-20261102.md)と、
[状態復元・分岐の検証結果](verification/genesis-history-20261102.md)を参照。

- `relay/backends/genesis.py`：Genesis専用処理。
- `configs/relay/genesis-panda.json`：CPU・周期・画像サイズの設定。
- `tests/test_genesis_backend.py`：契約検査と実物理の操作・画像・開閉・記録・停止・次の試行。

```bash
uv pip install --python .local/genesis-sim/bin/python pytest
RELAY_TEST_GENESIS=1 .local/genesis-sim/bin/python -m pytest \
  tests/test_genesis_backend.py tests/test_genesis_history.py -q --disable-warnings
```

`RELAY_TEST_GENESIS=1`なしでは実Genesisのテストをskipし、SDK不要の契約・CLIテストだけを実行する。

公式資料：[GenesisのIKと関節制御](https://genesis-world.readthedocs.io/en/latest/user_guide/robot_control/inverse_kinematics_motion_planning.html)、
[座標・quaternion規約](https://genesis-world.readthedocs.io/en/latest/user_guide/configuration/conventions.html)。
状態復元は[公式checkpoint仕様](https://genesis-world.readthedocs.io/en/latest/user_guide/configuration/checkpoints.html)も参照。
