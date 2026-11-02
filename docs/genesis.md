# Genesis：キーボード操作と記録

Genesis 1.4.5のPandaアーム・床・箱を、Relayの共通backend経由で動かす。
最初の対応範囲はキーボード操作、2カメラ表示、実行した指令・観測の保存、新しい試行。
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
- 新しい試行は`trials/0002/trajectory/`等に分け、前の記録を上書きしない。

操作を自動テストする場合は`--test-operator`を付けて`TEST_OPERATOR`と記録する。
利用者が操作する通常起動では付けずに`HUMAN`として保存する。
品質確認・学習投入は別工程で、保存しただけでは`training_admitted=true`にしない。

今回のGenesis backendは**checkpoint・巻き戻し・分岐・保存セッションの再実行復元には未対応**。
診断用の数値状態ベクトルは保存するが、完全なGenesis状態として復元できるとは扱わない。
タイムラインは無効にし、機械的な`replay_actions()`コードも生成しない。

## 実装と確認

[今回の検証結果](verification/genesis-keyboard-20261102.md)：専用環境17件、既存環境の回帰76件、JavaScript12件。

- `relay/backends/genesis.py`：Genesis専用処理。
- `configs/relay/genesis-panda.json`：CPU・周期・画像サイズの設定。
- `tests/test_genesis_backend.py`：契約検査と実物理の操作・画像・開閉・記録・停止・次の試行。

```bash
uv pip install --python .local/genesis-sim/bin/python pytest
RELAY_TEST_GENESIS=1 .local/genesis-sim/bin/python -m pytest \
  tests/test_genesis_backend.py -q --disable-warnings
```

`RELAY_TEST_GENESIS=1`なしでは実Genesisの4テストはskipし、SDK不要の契約・CLIテストだけを実行する。

公式資料：[GenesisのIKと関節制御](https://genesis-world.readthedocs.io/en/latest/user_guide/robot_control/inverse_kinematics_motion_planning.html)、
[座標・quaternion規約](https://genesis-world.readthedocs.io/en/latest/user_guide/configuration/conventions.html)。
