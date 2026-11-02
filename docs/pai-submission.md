# PAI最終課題：提出用の説明

作品名：Relay - 自律操作の「ここだけ」を直し、経路を残す。
OmniCampusの提出名：takumiecd。

## 締切延長後の改良版

利用者から最終課題の締切延長が伝えられたため、2026-11-02に資料を更新した。
新しい締切の時刻は未確認。元の提出タグ`pai-final-20261102`とPDF・動画は保持する。
以下の「元の提出版」の範囲と、改良版を混同しない。

改良版は、LIBERO／OSC、Genesis CPU、ROS 2／MoveIt／RViz参照モデルに対応する。
Genesisで実LLMの生成・物理動作・手動引き継ぎ・巻き戻し・分岐採用を撮り直し、
各環境の実VLA接続は検証済みの記録と出所を明示して示す。
ROSは数値参照controllerと表示用6軸モデルで、物理・接触・衝突回避はない。
Gazebo・実機・ゲームパッドは未対応。

[改良内容と実測](verification/submission-polish-20261102.md)に失敗例と検査範囲を残す。

```bash
python scripts/build_pai_polished.py
```

このscriptはローカルの実測記録・実画面・動画を必要とする。Git cloneだけで映像を合成できるものではない。
出力は`output/pdf/polished/説明資料_PAI最終課題_takumiecd.pdf`、
`output/submission-polished-takumiecd/PAI最終課題_takumiecd.mp4`。
`deliverables.json`へ検証値と素材／完成物のhashを保存する。
アップロード／提出は別操作。元のYouTube URLが新しい映像に差し替わることはない。

## 元の提出版（pai-final-20261102）

### 課題条件と当初の選択

10/19改訂版ではROS・Gazebo・Genesisを想定しつつ、他のツールと実機も使用可。
AIの利用は必須で、推論のみでもよく、学習は任意。
今回の実装はPython、CaP-X、LIBERO／MuJoCo／robosuite OSCを使う。
ROS 2、Gazebo、Genesis、ゲームパッドは提出の実装範囲に含めない。

GENIAC PrizeなどのCode as Policy型の利用は開発動機・将来目標。
特定のコンペSDKへ対応済みとは記載しない。

## 既存部品と追加実装

- 既存：CaP-Xのコード実行器、LIBERO、MuJoCo、robosuite OSC、事前学習済みOpenVLA。
- 追加：VLA共通adapter・通信口、制御主体の切替・停止、キーボードUI、制御器を含む状態復元、分岐図、採用経路の固定・書き出し。
- 実装場所：`capx/extensions/vla/`と`capx/extensions/hybrid/`。
- 検証の出典：`docs/verification/`。

## 提出後の拡張案

[ROS 2・Gazebo・Genesisの接続位置](environment-adapters.md)を、説明PDFの4ページ目にも図示する。
GenesisはPython APIへ直接、ROS 2はtopic／action通信を通してGazebo・実機の制御系へ接続する案。
現時点の動作確認はLIBERO／OSCのみであり、図の追加接続は未実装。

## 映像と説明の範囲

実OpenVLAの記録は`output/vla-live-02/osc-vla.mp4`。
SHA256は`a4432dbdd07617ffaf9d9434f48a2f30e912ba4df48c123ef64f76fc5cfc049f`。
30推論・30指令・手先の始点終点距離約0.269 m。把持・配置成功は未確認。
実モデル動画は物理時間基準の0.05倍スローと明記し、推論待ちは省略する。

後半は試験providerと自動キー入力によるUI・記録機能の確認。
実VLA・実LLM・実ユーザーの連続した共同操作を検証した映像ではない。
キー入力の再現は0.25倍スローと明記する。実演の27stepと、親の80stepを合わせた107stepを選択。
全記録で実入力とTEST／CODE_REPLAYを区別し、学習投入は行わない。

## 提出するもの

- PDF：`説明資料_PAI最終課題_takumiecd.pdf`。
- 動画タイトル：`PAI最終課題_takumiecd`。1〜3分のMP4をYouTubeへアップロードし、URLを提出する。
- YouTubeは限定公開以上。「子ども向け」「年齢制限」はいいえ。プレミア公開は使わない。
- GitHub：<https://github.com/takumiecd/relay>。
  公開設定はPUBLIC。mainへ追加実装を統合し、提出時はリポジトリのトップURLを指定する。
  このリポジトリはCaP-Xを拡張したforkであり、独立した環境共通基盤への分離は今後の作業。
- 氏名・所属・メールアドレスは掲載しない。OmniCampusアカウント名のみ使用する。
- 課題PDF記載の締切：2026-11-02 AM10:00。提出受付の状態は別途OmniCampusで確認する。

## 資料の再作成

ビルドにはreportlab、Pillow、ffmpeg／ffprobeが必要。GUI画像は実画面から取得したものを使う。

```bash
python scripts/build_pai_submission.py --account takumiecd
pdftoppm -scale-to 1400 -png output/pdf/説明資料_PAI最終課題_takumiecd.pdf output/submission-takumiecd/report
```

ローカルの映像・画像・PDF・tokenはGitへ含めない。YouTubeアップロード・OmniCampus送信は別途行う。
