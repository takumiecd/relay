# CaP-XへVLAを接続する：最初の実モデルはOpenVLA

このブランチの追加機能。CaP-X本体の公開リリースに含まれる標準機能ではない。
ROS 2は使用しない。既存のrelay-teleop、PARC、過去の記録は変更しない。

## 処理のつながり

```text
CaP-X CodeExecutionEnvBase.step(Python文字列)
  → VlaApi.run_vla(指示、action上限)
  → 複数カメラ画像・ロボット状態を共通workerへ送る
  → 選択したモデルadapterがaction列を提案する
  → session/epoch/観測ID・action形式・範囲を照合
  → OscLiberoEnv.apply_native()
  → 同じLIBERO-PRO環境のOSC_POSEへ適用
  → 新しい画像・状態を取得して、次の推論／Pythonへ戻る
```

シミュレータは一つだけ。生成コードの操作APIとVLAは同じ物理状態を使う。
HTTP threadやGPU workerは物理を進めず、作成threadだけがstep/観測/終了を扱う。
`pause()`はepochを更新するので、停止前に要求した推論が後から届いても適用しない。
人の入力UI・三者の切替UIはこの段階ではまだ追加していない。

実測結果は[verification/vla-osc-20261102.md](verification/vla-osc-20261102.md)を参照。

## コードの場所

| ファイル | 責任 |
|---|---|
| `capx/extensions/vla/env.py` | CaP-X BaseEnvを実装したOSC環境。単一step入口、観測、停止、GL context |
| `capx/extensions/vla/api.py` | CaP-XのAPI登録、`run_vla`、手先移動・gripper操作 |
| `capx/extensions/vla/bridge.py` | 認証付きreverse mailbox。画像と推論結果の対応付け |
| `capx/extensions/vla/worker.py` | モデルに依存しない推論worker。adapterのロードと通信 |
| `capx/extensions/vla/gpu_setup.py` | Colab/Jupyter内に独立したPython 3.10環境を用意（依存ファイルを指定可能） |
| `capx/extensions/vla/demo.py` | CaP-Xの実行器を通す有限デモと映像・実行ログ |

上流への変更は主にoptional dependencyの遅延import。既存のtask/API登録内容は
`capx/envs/tasks/_builtins.py`・`capx/integrations/_builtins.py`へ内容を維持して移した。
明示的に登録した追加APIだけを使うときは、SAM・Open3D・実機driverなどを読み込まない。
標準のAPI/task検索・一覧取得時には従来の登録を読み込む。
動的なAPI再登録ではfactory cacheも無効化し、別taskへ古いpolicy接続を流用しない。

GitHub上のfork作成は不要で、現在は上流cloneのローカルbranchで作業している。
本実装には小さなcore変更も含むため、「上流を全く変更していないplugin」とは扱わない。

## VLA共通部分とモデル固有部分

`api.py`・`bridge.py`・`worker.py`にはOpenVLAの推論処理を置かない。
`adapters.py`がモデルの拡張口、`codec.py`が画像・状態の共通通信形式、
`models/openvla.py`が最初の実装である。環境の制御契約は`contracts.py`に明記する。

共通入力は`images`（名前付きRGB uint8画像群）、`state`、`image_convention`と自然言語指示。
現在のLIBERO環境はagentview・wrist、関節角(rad)、手先姿勢(XYZ m + WXYZ)、
gripper位置(m)を渡す。使うカメラや状態、時系列履歴、画像前処理は各adapterが選ぶ。
OpenVLA adapterはagentviewだけを使うが、共通経路は手首画像とstateも運ぶ。

共通出力は`ActionChunk(actions, action_contract, diagnostics)`。
単一actionだけでなく、1〜100ステップのaction chunkを受け取れる。
chunkは全体を検査してから実行し、呼出し予算を超える分と停止後の残りは捨てる。
chunk内部は同じ推論結果を順に実行するopen-loop動作で、毎step再推論する意味ではない。
推論のたびに最新観測を渡す。session・epoch・観測IDで別sceneや停止前の結果を拒否する。

外部adapterは次の形のクラスを自分のPython packageへ追加する。

```python
from capx.extensions.vla.adapters import ActionChunk
from capx.extensions.vla.contracts import LIBERO_OSC

class MyVlaAdapter:
    def __init__(self, checkpoint):
        self.policy = load_my_policy(checkpoint)  # 利用モデルの公式SDKなど
        self.metadata = {"model": checkpoint, "action_contract": LIBERO_OSC}

    def predict(self, observation, instruction):
        output = self.policy.predict(observation, instruction)
        # このモデルの正規化・座標系・gripperをLIBEROのnative OSCへ変換する。
        actions = decode_for_libero_osc(output)
        return ActionChunk(actions, LIBERO_OSC)
```

`load_my_policy`と`decode_for_libero_osc`は各モデルに合わせて実装する関数であり、
上の例だけで未知のcheckpointが動くわけではない。
GPU workerへ`--adapter my_package.my_adapter:MyVlaAdapter --adapter-config config.json`
を指定すれば、共通実行側を編集せずロードできる。
config JSONはconstructor引数（例：`{"checkpoint": "..."}`）である。

Colab/Jupyter helperにも同じ指定を渡せる。

```python
run_worker(connection, root, output, adapter="my_package.my_adapter:MyVlaAdapter",
           adapter_config={"checkpoint": "..."},
           requirements="/path/to/my-vla-requirements.txt",
           environment_name="my-vla-policy")
```

外部packageはsource root内に置くか、その依存ファイルからinstallする。
配布ZIPに自動で含むのは共通workerと組込みOpenVLA adapterだけである。
モデルごとに依存を分け、OpenVLA用torch/transformersを他のVLAへ強制しない。
Python 3.11等が必要なモデルは、自分の環境で`python -m capx.extensions.vla.worker`を起動できる。
OpenVLA用の既定依存ファイルは`requirements-openvla.txt`である。

**現在の実環境backendはLIBEROのrelative OSC、実モデル確認はOpenVLAのみ。**
関節角を出すVLAなどには別の制御adapter/backendが必要で、同じ7要素でも接続可能とは限らない。
Pi0/SmolVLA等のモデル別adapter・学習済みcheckpointの互換性はまだ検証していない。

## 固定する条件

- 上流CaP-X：`53e9966d7a8e2fa7494676772bccc35280f5c0ed`を開始点にした。
- LIBERO-PRO：上流submodule指定の`5368540790fde9a18584d64105ec5ac16d8926bd`。
- 初回の動作確認：Spatial task 0、init 0、20 Hz、OSC_POSE、fixed impedance、relative。
- OpenVLA：`openvla/openvla-7b-finetuned-libero-spatial`。
- 重み：`962318cec55ac10993ff0f5f43eda9a270b4c873`。
- remote code：`47a0ec7fc4ec123775a391911046cf33cf9ed83f`。
- `unnorm_key=libero_spatial`、crop面積0.9、BF16、SDPA、seed 7、量子化なし。
- controllerの入力・出力倍率、kp/kd、周波数を`report.json`へ保存する。

今回のOpenVLA adapterの出力は手先位置変化3・向き変化3・gripperの7要素。
画像はraw LIBERO conventionで送り、OpenVLA adapter内で公式の180度回転・JPEG・resize・cropを行う。
モデルのgripper値からLIBEROの符号への変換はOpenVLA adapterで一度だけ行う。
手先回転はWORLD座標系のaxis-angle。Euler角をそのまま足し引きしない。

この接続試験は公式大会の評価ではない。成功率、汎化、学習改善は報告しない。
通常LIBERO用checkpointをLIBERO-PROへ接続した結果も、標準LIBERO評価と同一とは扱わない。

## 手元の実行環境

上流の全依存と分けて、小さなsim専用環境を作る。Macでの確認はPython 3.10。

```bash
git submodule update --init capx/third_party/LIBERO-PRO
uv venv --python 3.10 .local/openvla-sim
uv --no-config pip install --python .local/openvla-sim/bin/python \
  -r capx/extensions/vla/requirements-sim.txt
```

初期状態などのsource assetはsubmoduleを利用する。巨大な学習datasetは不要。
LIBEROのpath設定は各output内に作り、`~/.libero`や他repoの設定は変更しない。
Macの古いMuJoCoライブラリ参照はimport中だけ補正し、インストール済みpackageを書き換えない。
`--no-config`で上流projectの依存overrideを適用せず、このsim専用の固定依存を使う。

### GPUなしで実行経路だけを確認

```bash
.local/openvla-sim/bin/python -m capx.extensions.vla.demo \
  --test-input --max-actions 10 --output output/osc-test
```

これは実VLAではなく、固定の試験指令。`TEST_VLA`と記録し、人の入力やモデル生成とは区別する。

### 実OpenVLAで実行

```bash
.local/openvla-sim/bin/python -m capx.extensions.vla.demo \
  --max-actions 30 --output output/openvla-live
```

`output/openvla-live/connection.json`ができ、GPU接続を待つ。
同一マシンならそのままloopbackを使う。別マシンなら、推論専用port 8896への
SSH転送や認証付きtunnelを用意し、`--public-url`でGPUから到達できるURLを指定する。
このportは推論専用で、環境を操作するHTTP endpointは提供しない。
connection JSONは秘密のtokenを含む。Gitへ追加せず、GPU環境だけへ転送する。

GPUへ渡す小さなsource ZIPは次で作れる。重み・録画・tokenは入らない。

```bash
python3 scripts/package_vla_worker.py --output output/vla-worker.zip
```

[notebooks/vla_worker.ipynb](../notebooks/vla_worker.ipynb)をColabまたはLinux GPU Jupyterで開ける。
ZIPとconnection JSONを置き、セルを上から実行する。以下はその中心となる呼出し。
Notebookを使わない場合も、ZIPを展開し、そのrootとconnection JSONを指定する。
Notebook本体にはtorchを追加せず、下の関数が専用venvのworkerを起動する。

```python
import json
from pathlib import Path
from capx.extensions.vla.gpu_setup import run_worker

run_worker(
    json.loads(Path("connection.json").read_text()),
    root=Path.cwd(),
    output=Path("output/policy"),
    seconds=600,
)
```

source rootをPythonのimport pathに入れてからこのセルを実行する。
Mac側の処理完了でworkerへ終了を通知する。Notebook cellの完了だけでGPU割当を解放したとは
扱わず、Colab/Jupyterの割当終了も行う。共有Colab poolを使う場合は専用queue経由とし、
queue所有runtimeへ手動で接続しない。

## 確認する出力

- `report.json`：実際の適用数、前後の手先位置、モデル・controller設定、エラー。
- `actions.json`：actionごとの出所、前後の観測ID、epoch、実際の手先変化。
- `executed-code.py`：CaP-Xへ渡したPython文字列。
- `before.png` / `after.png` / `wrist-after.png`：目視確認用。
- `osc-vla.mp4`：適用後の画像。20 fpsのsim時間で再生し、推論待ち時間は含まない。

HTTPの成功や`run_vla`のreturnだけで物理動作を認めず、action traceと手先変化・画像を照合する。
action予算での停止はタスク成功ではない。人の教師データとしては出力しない。

## データセット作成へ進める方針

[dataset-design.md](dataset-design.md)に、共通記録、数値再生用コードへの変換、
VLA用とコード生成VLM用データの区別をまとめた。`--record-dataset`で両カメラ・状態・
実行済みactionを保存できる。再生は元の操作主体と区別して記録する。

## テストと現在の制約

```bash
.local/openvla-sim/bin/python -m pytest tests/test_vla_extension.py -q

CAPX_LIBERO_SOURCE=capx/third_party/LIBERO-PRO \
  .local/openvla-sim/bin/python -m pytest tests/test_vla_extension.py -q
```

二つ目は実シミュレータを作成して、無効な指令の拒否、実際の移動、停止後のcontext拒否も確認する。

`move_to_pose`は近傍の手先目標へOSCで近づける処理で、衝突回避plannerではない。
関節軌道APIは公開せず、特定の関節姿勢やnull-spaceの再現を約束しない。
三者のUI、採用済みデータの学習formatへの変換、物体認識API、MCP/skill、LLM自身の推論接続は次段階。
CaP-X上流のPython executorは任意importと低レベルenvへのアクセスを許す。今回の停止は
追加した操作APIを通す処理で有効であり、任意のPythonを強制停止できるsandboxではない。
