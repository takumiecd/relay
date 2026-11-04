# 手動操作の基準選択（2026-11-04）

LIBEROの手動操作に、ワールド・全体カメラ・手首カメラの選択を追加した。
全体カメラではWASDを机の平面へ投影し、手首カメラでは現在の画像平面へ合わせる。
既存の表示画像の反転も方向変換へ反映する。R/FはWORLDの高さ、回転キーはWORLDのまま。
従来のAPI入力は`reference`省略時にワールドを使う。

## 自動確認

sim専用Python環境で実行：

```bash
python -m pytest tests/test_teleop_reference.py tests/test_hybrid_control.py \
  tests/test_backend_runtime.py tests/test_backend_boundary.py tests/test_vla_extension.py \
  tests/test_runtime_extraction.py tests/test_ros2_backend.py tests/test_ros2_moveit.py \
  tests/test_genesis_backend.py -q --disable-warnings
```

88 passed / 36 skipped。この時点では実LIBERO・Genesis・ROS 2の追加環境を指定していない。
スキップした環境の動作確認を意味しない。

```bash
CAPX_LIBERO_SOURCE=/path/to/LIBERO-PRO python -m pytest \
  tests/test_teleop_reference.py tests/test_hybrid_control.py tests/test_vla_extension.py \
  -q --disable-warnings
```

実LIBEROを含む61件がpass。その後追加した
`test_real_camera_reference_conversion_is_read_only_and_tracks_the_wrist`も単独でpass。
同テストでは実カメラ姿勢による方向、実際の手首回転への追従、変換前後の物理状態・policy画像の一致を確認した。
入力の順序・操作権・Space解除、記録の基準名、native VLA actionを変換しないことも検証した。

```bash
node --test tests/web/*.test.mjs
```

12件pass。既存のキー操作と分岐図の回帰確認。

## ブラウザ確認

別のローカルLIBEROサーバーを`--manual-only --test-operator`で起動し、Chromeで確認した。

- 初回は全体カメラが選択され、3種類の基準と対応する説明が表示される。
- 手首カメラへの切替とSpace+Dの実動作を確認。
- ワールドへの切替後、再読み込みしても選択が保持される。
- 全体カメラでSpace+Wの実動作とEsc停止を確認。ブラウザのエラーログは0件。
- 記録5 stepはすべて`TEST_OPERATOR`で、手首4 step・全体1 stepの基準名とnative actionを保持。

確認用サーバーは停止済み。既存の4090収集サーバー・記録・VLA workerは変更していない。
これは入力・変換・記録の確認であり、タスク成功率や操作遅延改善の測定ではない。
