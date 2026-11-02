# Genesis LLM Code as Policy — 2026-11-02

対象：`feat/genesis-llm`。提出版tagは変更しない。
PR #3（CPU Genesisの状態復元・分岐）はmainの`ccaed7dad7809d85567df71308f7efd270828350`へマージ済み。

## 変更

- 画面から操作指示を渡し、現在の画像と状態をHTTPのChat Completions APIへ送る。
- モデル非依存の生成adapterを維持し、GPU／商用APIをURL・model・環境変数のキーで選ぶ。
- 生成コードと指示、実行状態・actions数を表示。手入力コードは別欄のまま残す。
- LLMだけの`--no-vla`起動、HTTP timeout・キー環境変数名の指定を追加。
- 準備済みVLAがないときは`run_vla`を提示・実行可能なツールから除く。
- 要求した指示をCODE操作・program metadata・VLA/VLM-code候補へ引き継ぐ。
- `llm-requests/`に生成要求と受信／破棄／エラーを記録。停止後の返答は実行しない。
- Genesisのグリッパー単独操作は最後の手先目標を維持し、測定姿勢の沈みを繰り返し積算しない。

## 自動検証

LIBERO環境で既存のbackend・VLA・hybridと新規LLMテストを実行：
**91 passed, 19 skipped, 15 warnings**（新規の異常応答テスト追加前）。
skipは専用Genesisランタイムを要求するケースで、物理の成功とは数えない。
新規LLMテストだけの追加確認：**10 passed, 1 skipped**。

Genesis 1.4.5 / CPU / macOS専用環境で、backend・checkpoint・LLM接続の統合を確認：
**42 passed, no skips**（異常応答テスト追加前）。
最終確認（異常応答・要求記録・モデル出所・範囲外目標のエラー表示を含む）は **44 passed, no skips**。

JS：キーボード・分岐図 **12 passed**、console.js構文検査、git diffの空白検査。
ノートブックは全Pythonセルの構文検査を実行。vLLMレシピの起動試験とは区別する。

重要な検査は、HTTP要求の指示／画像→生成Python→実行→CODE出所→dataset候補までの一致、
LLM待ち中のSTOP/HUMAN切り替えと遅い返答の破棄、無効な返答での停止と自動再試行なし、
Genesisの物理移動・開閉・グリッパー目標保持・その経路の完全な状態復元。
HTTPテストには明示したfixture応答を使い、実モデルの結果と分ける。

## L4実モデル：生成品質と実行結果

Genesisの実際の初期観測（手首・全体のRGB画像2枚、手先・関節・箱の真値座標）と、
その時点のツール仕様・英語の短い操作指示を入力した。純粋なvision-only制御ではない。
GPU上ではモデル推論だけを行い、返答を回収して手元のGenesisで実行した。
**このL4試験はライブHTTP接続の試験ではない。** 実行時は`generator=recorded_llm_response`、
`inference_origin=Colab L4 offline validation`として区別し、人間のデモとは扱わない。

共通ランタイム：NVIDIA L4 24GB、Torch 2.11.0+cu130、Transformers 4.57.1、BF16、SDPA。
初回のvenvセットアップはensurepip不足で失敗したため、job専用venvへpipをbootstrapして再提出した。
既存のGPUジョブやsystem-wideパッケージは変更していない。

| モデル・プロンプト | 観測した結果 |
|---|---|
| Qwen2.5-VL-3B、最初の仕様 | 引数の重複・7要素pose・相対値を絶対座標とする誤り |
| Qwen2.5-VL-3B、絶対XYZとコード例を明示 | 未定義変数や座標の誤りが残る |
| Qwen2.5-VL-7B、明示した仕様 | 「上に2cm→閉じる」は実行可能なコードを生成。別の-X移動は誤り |

7Bの「上に2cm→閉じる」を初期状態から実行：**50 actions**、例外なしでプログラム完了。
手先の実移動XYZは`[+0.0003243, +0.0000310, +0.0154940] m`、
指の最終位置は左右とも約`0.000240 m`。目標2cmへの3mm以内の到達を確認した結果ではない。
移動の40actionsを使い切った後に開閉が続いた。コード完了と目標到達／タスク成功を区別する。

別の-X移動の返答は`open_gripper(40)`を行った後、現在位置から20cmを超える絶対座標を要求した。
**40 actionsの開動作を記録したうえで、移動は範囲検査により拒否、program_errorで停止。**
トランザクションのrollbackではなく、エラー前の物理動作もraw記録に残る。

上の少数例は局所的な接続／実行試験であり、成功率・把持能力・未知指示への汎化を示さない。
実応答・モデルrevision・source digestは[artifacts/genesis-llm-20261102/](artifacts/genesis-llm-20261102/)に保存。
元のpool receipt・固定source・実行ログはホストのpool job directoryに保持している。

## UI確認

`output/genesis-llm-ui-qa-20261102`：実Genesisに固定のTestGeneratorを接続して、
指示欄→生成コード表示→物理step→CODE経路をブラウザで確認した。30actionsで上昇を表示。
`fixed_test_program`／「試験コード」の表示を保ち、実LLMデモとは混同しない。
ブラウザのconsole errorは0。スクリーンショットは同directoryの`console.png`。

## 制限

- モデルは誤ったコードを生成しうる。今回の少数の実モデル例でも誤りを確認した。
- boundedなツール実行・epoch・停止・作業域検査はあり、衝突回避や実機の安全認証ではない。
- Genesisの箱の成功評価器はなく、自律把持／タスク完了を主張しない。
- VLA実モデルのGenesis用action変換は今回の対象外。
- CPU checkpointは同じ稼働中scene/reset内に限定し、再起動をまたいだ復元は未対応。
- 生成コードは機械的なtrajectory replayとは別の候補として保存し、学習採用は自動化しない。

## Macの実API接続と利用用セッション

MLX-VLM 0.7.11 / MLX 0.32.4、Apple Silicon Metal、
`mlx-community/Qwen2.5-VL-7B-Instruct-4bit` revision `fdcc572e8b05ba9daeaf71be8c9e4267c826ff9b`。
専用`.local/llm-mlx`へインストールし、依存整合性検査も通過。
APIは`127.0.0.1:8000`だけで待ち受け、Genesis側から通常のHTTP要求を送った。

`output/genesis-llm-live-qa-20261102`でブラウザから次を確認した。

1. 日本語で「現在位置から上に2cm→10steps閉じる」：実LLMが正しいAPIのコードを生成、
   **50actions**を実行し停止。指示・コード・モデル・画像とCODE操作を保存。
   上昇量と追従誤差は上のL4返答実行と同じ範囲で、目標到達保証とは扱わない。
2. 日本語で「10steps開く」：**10actions**、プログラム完了。
3. さらに移動・開閉のコード生成を要求して、生成待ちの間にEscを入力。
   返答は`discarded`として記録し、**追加actionsは0**、総step数は60のまま。

このQAは`--test-operator`を付け、代理のキーボード入力を人間デモとしない。
`--test-llm`は付けず、モデル出所は実際の`llm_http`として記録している。
結果のJSONは[mlx-live.json](artifacts/genesis-llm-20261102/mlx-live.json)、
スクリーンショットは同QA directoryの`console.png`。

QAを保存して停止し、利用用には別の`output/genesis-llm-user-20261102`をポート8878で起動。
利用用は`--test-operator`を付けず、以後の利用者による手動操作をHUMANとして記録する。
既存8877の33stepの操作セッションは停止せず保持した。
Colabの実験用L4はすべて回収・停止済みで、常駐させていない。
