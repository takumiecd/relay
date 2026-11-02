# Genesisキーボード操作・記録の確認（2026-11-02）

対象は`feat/genesis-backend`、土台は共通backendを取り込んだmain `08484d6`。
起動方法は[Genesisガイド](../genesis.md)、環境・版・主要ソースのSHA-256・実測値は[JSON記録](genesis-keyboard-20261102.json)を参照。

## 確認した範囲

- macOS Apple Silicon、Python 3.12、Genesis 1.4.5、CPU物理計算、offscreen描画。
- Pandaの手先指令をGenesisのIKで関節目標へ変換し、PD制御と物理stepで動作。
- 手首／全体カメラ、速度変更、Space deadman、C/Vの開閉、Esc停止、記録、新しい試行。
- nativeの手先目標に加え、IKの9関節制御目標・誤差・物理step数を記録。
- キーを離している間はシミュレーション時刻・物理状態・記録stepが進まない。
- deadmanの解除・再開を繰り返しても、重力でずれた実測角へ目標を繰り返し置き換えないことを確認。

SDK環境で`tests/test_genesis_backend.py`は**17件成功、skipなし**。
既存LIBERO環境で共通backend・VLA／コード切替・記録・分岐・実LIBEROを含む回帰検証は**76件成功**。
このコマンドではGenesis実物理の4件をskipし、上記専用環境で別実行した。
JavaScriptのキーボード・分岐図は**12件成功**。構文検査と`git diff --check`も成功。
専用requirementsを`uv pip install --no-config`で適用し、`uv pip check`で依存不整合なしを確認した。

## ブラウザからの操作

8878の試験セッションで、実際のキー入力から63 stepを実行。
H→Space+Wで移動、Escで停止、C→Spaceで閉じる、V→Spaceで開く動作を確認した。
すべて`TEST_OPERATOR`と記録し、利用者のHUMAN操作に混ぜていない。
閉じた指の位置は左右とも約0.00000125m、開いた位置は左右とも約0.039998m。
raw観測・両カメラ画像・指令・関節制御目標が保存され、終了時にexportされた。

このブラウザ試験後にPDのhold目標保持を調整し、最終版は専用環境でdeadman解除・再開の実物理テストも含めて17件を再確認した。
最終版の利用者用セッションは8877に別途起動し、両カメラ表示、step 0、停止状態を確認。
このセッションは`--test-operator`なしで、利用者による以後の操作をHUMANとして記録する。
checkpoint非対応のタイムラインは非表示となり、VLA／LLMは未接続で無効になっている。

## 検証に含まないもの

Genesisのcheckpoint復元・分岐・保存軌跡の厳密再実行、別robot／scene、CUDA／Metal、実VLA／LLM接続。
箱の把持成功・タスク成功率の評価は実装していない。記録データの学習投入も承認していない。
