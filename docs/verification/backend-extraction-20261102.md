# Backend分離の確認（2026-11-02）

対象は`feat/environment-adapters`。提出版`pai-final-20261102`とmainは更新しない。
構成・外部factoryの追加手順は[backend実装](../environment-adapters.md)を参照。
数値結果と主要ソースのSHA-256は[JSON記録](backend-extraction-20261102.json)に保存した。

## 変更前とのLIBERO比較

参照は`9a3d854e73c9852787d7f48d6e9ec5f05da99c45`の`OscLiberoEnv`。
同じmacOS専用venv、LIBERO-PRO source、seed 0、spatial task 0、96×96画像、初期settle 10 stepを使い、
参照実装と分離後の実装をそれぞれ生成・実行・closeした。

20指令は`[.06, -.02, .1, 0, .01, 0, gripper]`。
先頭8指令はgripper=-1、残り12指令は+1。
初期状態と各指令後の計21時点で、MuJoCo integration stateと手先XYZ/WXYZが配列の完全一致。
同じ21時点のagentview／wristの計42枚もraw画素の完全一致だった。

## 機能テスト

```bash
CAPX_LIBERO_SOURCE=capx/third_party/LIBERO-PRO \
  .local/openvla-sim/bin/python -m pytest \
  tests/test_backend_runtime.py tests/test_backend_boundary.py \
  tests/test_vla_extension.py tests/test_hybrid_control.py -q --disable-warnings
node --test tests/web/hybrid-keyboard.test.mjs tests/web/hybrid-graph.test.mjs
node --check capx/extensions/hybrid/console.js
```

Pythonは63件成功、skip 0、依存先の警告15件。JavaScriptは12件成功、構文検査も成功。
実LIBEROの記録・再実行・停止・グリッパー・カメラ・checkpoint復元・分岐を含む。
別途、8次元絶対手先指令の数値backendでもVLA形式／コード／手動の切替と採用経路固定を確認した。
checkpoint非対応時の記録・次の試行と、reset／手先移動非対応時の拒否も確認した。

## Web画面

検証用に`kinematic`を8877、外部`module:Factory`による3視点の数値backendを8878で起動した。
ブラウザで`reference_view`の1画面と`front_test`／`inspection_test`／`side_test`の3画面を確認。
全画像がロードされ、1画面は1列、3画面は主表示＋副表示2枚となり、JavaScriptエラーはなかった。
これらは数値参照画像であり、別の物理シミュレータや実モデルの映像ではない。

実GPU上のVLA／LLM推論、Genesis／ROS 2／Gazeboの接続、実機の停止試験は今回の検証に含まない。
