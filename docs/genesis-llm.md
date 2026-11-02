# GenesisをLLMのPythonコードから操作する

操作画面に短い指示を入力し、LLM/VLMが生成したPythonをCaP-Xの実行器で実行する。
Genesis backendに対してもLIBEROと同じツールAPIを使い、物理計算は手元のowner threadに限定する。

```text
画面の操作指示＋その時点の画像／状態
  → HttpCodeGenerator → GPUサーバー／商用APIのChat Completions
  → Pythonコード
  → CaP-Xの別プロセス実行器
  → move_to_pose / open_gripper / close_gripper
  → GenesisのIK → 関節PD → 物理step → 画像と操作の記録
```

## 起動

Genesis専用環境は[genesis.md](genesis.md)の手順で準備する。
LLMはGenesis側に読み込まず、別のAPIとして接続する。
GPUサーバーのJupyter／Colabでは[llm_server.ipynb](../notebooks/llm_server.ipynb)に
vLLM起動・認証・SSHまたは一時トンネル・停止までの例を用意している。
このvLLMレシピは実験のTransformers直接推論と区別する。
既にOpenAI互換APIがあればノートブックは不要。

```bash
# キーが必要なAPIでは、端末で入力する。ソースや起動引数には書かない。
read -s CAPX_LLM_API_KEY
export CAPX_LLM_API_KEY

.local/genesis-sim/bin/python -m capx.extensions.hybrid.server \
  --backend genesis --backend-config configs/relay/genesis-panda.json \
  --no-vla \
  --llm-url http://127.0.0.1:8000/v1/chat/completions \
  --llm-model Qwen/Qwen2.5-VL-7B-Instruct --llm-images \
  --llm-timeout 120 --port 8877 --output output/genesis-llm-01
```

URLは`/v1`だけでなく**`/v1/chat/completions`まで**指定する。
GPUが別のサーバーならSSHポート転送を使うか、認証したHTTPSエンドポイントへ変更する。
モデル名は接続先に合わせる。RelayのコードはQwen専用ではない。
テキストLLMには`--llm-images`を付けず、状態と指示だけを送る。
キーの環境変数名は`--llm-key-env`で変更できる。

`--no-vla`ではVLA bridgeと`connection.json`を作らない。LLMとキーボードだけで操作できる。
`--manual-only`はLLMも無効にするため、ここでは使わない。
VLAを後で追加する場合は`--no-vla`を外し、そのbackendのaction契約に対応するworkerを接続する。
LIBERO用OpenVLAの7次元相対指令をGenesisの8次元絶対目標へ直接渡すことはできない。

## 操作

1. `http://127.0.0.1:8877/`の「LLMへ指示」に短い目標を入力する。
   最初は「現在位置から手先を上に2cm動かして、グリッパーを閉じてください」など。
2. 「指示を実行」または、入力欄からフォーカスを外してLを押す。
3. 生成中→コード実行→停止の順で進む。「生成コードと実行結果」を開いて内容を確認する。
4. Escで生成待ち／実行中とも停止。Hで手動へ切り替える。
   切り替え前の推論が後から届いても実行しない。
5. 履歴のstepへ戻り、別の指示や手動操作で続けると分岐として残る。

「API設定済み」は接続先を設定した状態を表す。接続確認やモデルの推論成功を意味しない。
生成に失敗した場合は停止してエラーを表示する。自動で再試行して動かすことはしない。
手入力したPythonの実行欄は従来どおり別に残し、`operator_code`として区別する。

## ツールと範囲

- `get_robot_state()`：現在の手先XYZ＋WXYZ、関節角、グリッパー状態。
- `move_to_pose([x,y,z], quaternion_wxyz=None, max_actions=...)`：絶対WORLD座標[m]。
  相対移動では現在位置を読んで足し引きする。現在位置から20cm以内、最大100actions。
- `open_gripper(steps=...)`／`close_gripper(steps=...)`：1〜40actions。Genesisでは手先目標を保つ。
- `run_vla(...)`：準備済みVLAがある場合だけ提示・実行可能。LLMだけの起動では提示しない。

Python実行は30秒、全体で100actionsを上限とし、owner側で権限とepochを確認する。
衝突回避計画や成功保証はない。現在は局所移動と開閉を確認するsandboxであり、自律的な箱の把持成功を主張しない。
Genesis観測は画像に加えて手先・関節・箱の真値座標を含む。純粋な画像だけによる操作評価ではない。

## 保存と検証

`trajectory/programs/<id>/`（分岐後は対応するbranchのtrajectory）に以下を保存する。

- `code.py`：実行したコード。
- `metadata.json`：モデル・接続先・指示・要求epoch・開始観測。
- `generation.json`：送信メッセージと実モデルの返答。
- `result.json`：完了／中断理由、実行actions数、終了context。

実際の各操作は`source=CODE`とプログラムID・モデル・指示を付けて記録する。
書き出すVLA／VLM-code候補も、シーンの既定文ではなくその操作指示を使う。
採用した経路も自動的には学習用データと認定せず、`training_admitted=false`のまま残す。

[検証結果と制限](verification/genesis-llm-20261102.md)を参照。
公式API仕様：[vLLM](https://docs.vllm.ai/en/v0.11.0/serving/openai_compatible_server.html)、
モデル例：[Qwen2.5-VL](https://huggingface.co/Qwen/Qwen2.5-VL-7B-Instruct)。

## Apple SiliconのMacで試す場合

CUDA用vLLMの代わりに、既存の[MLX-VLMのAPIサーバー](https://github.com/Blaizzy/mlx-vlm#server-fastapi)を使える。
Genesisと分離した推論専用環境を用意する。

```bash
uv venv --python 3.12 .local/llm-mlx
uv pip install --no-config --python .local/llm-mlx/bin/python mlx-vlm==0.7.11
.local/llm-mlx/bin/python -m mlx_vlm.server \
  --host 127.0.0.1 --port 8000 \
  --model mlx-community/Qwen2.5-VL-7B-Instruct-4bit \
  --max-num-seqs 1 --min-pixels 100352 --max-pixels 200704 --vision-cache-size 4
```

Relay側は同じURLを使い、`--llm-model mlx-community/Qwen2.5-VL-7B-Instruct-4bit --llm-images`とする。
APIはloopbackだけにbindする。外部のGPUサーバーを接続する場合は、上のSSH転送か認証付きAPIを使う。
この4bit版とL4のBF16版は別ランタイムであり、生成結果が一致するとは扱わない。
