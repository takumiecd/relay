# 提出改良版：2026-11-02

対象：`feat/submission-polish`。PR #7のmain `8e40697`から追加。
最終課題の締切延長を受けた改良。元の提出タグ`pai-final-20261102`と参照タグ`ros2-reference-20261102`は変更しない。

## 手動操作と表示

- 指令を送る前のIK失敗／純粋な計算timeoutを型で区別する。手動モードは維持し、同じ入力の繰り返し計算を抑止する。逆方向へ変えるかSpaceを明示的に離すと再操作できる。
- heartbeatの失効だけで拒否入力を自動再試行しない。Esc／epochによる失効は維持する。
- 送信後の結果不明、古い測定状態、不正なIK解は回復対象にせず停止する。holdや実行自体のエラーも握りつぶさない。
- 「記録済み指令」をROS接続でも表示。物理の積分stepとは表記を分ける。
- 手入力コードの実行中は「Pythonコード」、実LLM生成中は「LLM生成中」。停止理由は日本語の要約と、開いて読める生の詳細に分ける。

回復試験7件：拒否中に指令0、同じ方向のheartbeatでは再計算なし、逆方向で復帰、Space解放で再試行、heartbeat切れだけでは再試行なし、Esc後の古い入力拒否、未知の例外を回復扱いしないこと。
ROS統合試験でも、存在しない実IK serviceのtimeout後、手動を保ち、元の実MoveIt serviceへ戻して逆方向へ動けることを確認した。

## MoveIt 2.5.10の終了クラッシュ

元の構成ではSIGINT後、MoveGroupはexit -11。launch自体はexit 0だった。
GDBでrclcppのCallbackGroup／Node破棄を含むstackを確認した。
executorとcontextの解放順だけの変更、RobotModelLoader保持だけの変更でもクラッシュは残った。これらの試行は製品コードへ採用しない。

最終変更は**上流のmove_group.cppを変更せず**、default capabilityライブラリを通常のリンク依存として保持するもの。
callbackの解放時に必要なコードをplugin loaderの寿命だけに依存させない。
aptのSDKライブラリを保ち、実行入口だけを`relay_moveit_runtime`としてビルドする。

- 上流：MoveIt tag `2.5.10`、`moveit_ros/move_group/src/move_group.cpp`。
- SHA256：`e5149cf2f3fd8d8006e33373b70f435bf3edec69b2744ea492bbcfba34f9a5e6`。
- Docker ADDと準備scriptで照合。BSD headerとLICENSEをイメージへ保持する。
- CMakeでMoveIt依存versionを2.5.10 EXACT指定。異なるSDKでの成功を推定しない。
- 比較用：`move_group_package:=moveit_ros_move_group`でapt実行入口を選択可能。

新イメージ・未使用domainで、起動→実IK→1mmの関節指令→SIGINTを3回反復。
3回ともcontroller／robot_state_publisher／MoveGroupが正常終了し、exit -11もTracebackもなし。
`scripts/check_moveit_shutdown.py`はlaunchだけでなく**各子プロセスの終了ログ**を検査する。
検証用コンテナへ`relay/`と`scripts/`をread-onlyでマウントし、`--output /output/shutdown`を指定して実行する。

**class_loaderの解放警告は残る。** 警告を抑制しておらず、上流SDK全般の修正や実機向け安定性を証明しない。
GUI／物理／衝突等の未実装範囲も変わらない。

## 実LLMと手動・分岐の撮り直し

独立したGenesis CPU sceneを使用。既存の利用者セッションを巻き戻し・再起動しない。
実MLX HTTP API、`mlx-community/Qwen2.5-VL-7B-Instruct-4bit`、手首・全体の画像と数値状態を入力した。
`--test-operator`を付け、代理キー操作をHUMANとしない。固定のtest LLMは使わない。

最初の生成はsnapshotの`eef_pose_xyz_wxyz`を`get_robot_state()`のキーと取り違え、KeyErrorで**0指令**の停止。
失敗記録を`genesis-demo/`に保持。プロンプトとAPI説明で二つのschemaを区別し、別のscene `genesis-demo-v2/`で再確認した。
単一の修正例であり、モデルの正答率向上を測った結果ではない。

1. 「上へ5cm、max_actions=60」：実生成コードの60指令を実行。Zは0.580544→0.626096m（約4.56cm）。要求との差は約4.45mmで、3mm以内の到達は未確認。プログラム完了と目標到達を区別する。
2. H → Space＋W → Esc：48 TEST_OPERATOR指令、Xは0.411361→0.697337m（約28.6cm）。壁時計付きの実画面75frameを保存。
3. 分岐図のstep 60をクリック。再開直前の32要素physics_stateは元のstep 60とbitwise一致。SDK内部全配列・画像の一致を主張しない。
4. 別方向のSpace＋Aで40指令、Yは約15.4cm移動。元の108stepは保持。
5. CODE 60＋TEST_OPERATOR 40の100stepを採用。実行済みPythonの候補1件。全候補はtraining_admitted=false。

映像は複数の検証記録を編集する。LIBEROの実VLA映像、Genesisの実LLM／代理操作、ROS参照表示を、一回の連続した三者操作と誤認させない。
記録画像の再生・推論待ち省略・再生倍率を各区間へ記載する。
Genesisの実VLA三者接続は別の[10指令の試験](genesis-vla-20261102.md)で確認済みだが、把持・タスク成功・policy適応の評価ではない。

## 検査と保存先

| 対象 | 結果 |
|---|---|
| 実ROS 2／MoveIt／TF | 22 passed |
| Genesis CPU | 82 passed |
| 既存runtime・backend・VLA・記録＋手動復帰 | 111 passed, 2 skipped, 15上流warnings |
| キーボード・分岐図JS | 12 passed |
| LLM schema説明＋手動復帰の追加確認 | 18 passed |
| MoveIt終了反復 | 3/3 cycles、全3子プロセス正常終了 |

skipはGenesis専用runtimeが必要なケースで、Genesis環境で別途実行した。
生のログ・画面・記録は`output/submission-polish-20261102/`。
改良版の資料は元の提出物を上書きせず、`output/pdf/polished/`と`output/submission-polished-takumiecd/`へ作る。
生成scriptは`scripts/build_pai_polished.py`。元映像・採用経路・生成コードのhashと出所を検査してから作成する。
改良版は5ページのPDFと132.625秒・1920×1080・H.264の無音MP4。字幕で内容と再生倍率を示す。
PDF全ページ、動画の全11区間を目視確認。手動区間は実画面と両カメラの記録を拡大して示す。
最終sourceから再ビルドしたイメージでも、別domainの3 cyclesを再確認した。
数値・素材／完成物のhash・source commit・shipping imageは[機械可読の結果](artifacts/submission-polish-20261102/result.json)へ保存。
資料に記載したsource commitは生成元の`261bb5e`で、この後の検証JSON・説明追記とは分けて固定している。
YouTubeやOmniCampusへの再送信はこの作業に含めない。
