# 操作画面とSpace/Escの修正（2026-11-02）

## 観測した問題

- Spaceのdefault抑止がHUMANモードだけだったため、他のmodeではpageがscrollした。
- 起動中の試行は234 stepでtask_completedとなり、操作buttonが無効になっていた。
- 終了後に新しい試行を画面から始める手段がなかった。
- pageを再読み込みするとclient IDが変わる。HUMAN表示でも前のclientが操作権を持ち、入力を拒否された。
- mode要求の到着順しか見ていなかったので、遅れた開始要求が後の停止を上書きする余地があった。
- 最後に適用したstepの表示更新が、同時に受け付けた停止のmessageを上書きしていた。

現場で送ったEscはserverのevent logにも届いた。
停止済み／終了済みの状態で応答が見えない点と、停止後にrunning messageが残る点も修正した。

## 変更

- 停止buttonを常時見えるheaderへ置き、camera・操作panelをviewportに収めた。
- Pythonと操作guideは折り畳み、sidebarだけを必要に応じてscrollする。
- Spaceはkeydown/keyupともcaptureでdefaultを抑止。編集欄の通常の空白入力は維持する。
- Escはcode/key/legacy aliasを正規化して、編集欄でも停止を優先する。
- mode・code要求はclient sequenceとepochで照合。停止は古いsnapshotからでも受け付ける。
- pageごとの操作権を表示し、手動中にSpaceを押せば新しいpageへ引き継ぐ。
- focus APIだけに依存せず、受信した実keyとblur/visibilityで有効な入力を管理する。
- 入力packetは同時送信を抑え、Space releaseは待たせず送る。
- 停止とstep完了が競合しても、古いrunning表示を戻さない。
- 「新しい試行」は記録を保存してからowner threadでresetし、別directoryへ記録する。
- HTML/CSS/JSはsourceを分離し、reloadで新しいfrontendを取得できるようにした。

## 確認

- 旧記録`output/hybrid-console-ready-01/actions.json`に234 stepと終了flagを保持した。
- 新画面で停止中のSpace、H、Space+R、Escを実キーで操作した。
- 手動18 stepを適用後、Escで停止し「Escで停止しました」を表示した。
- VLAの試験入力中もEscで停止した。
- 70 stepの試行を保存して新しい試行へ進み、step=0／停止状態へ戻ることを確認した。
- HUMAN状態でreloadした後、Space+Rで操作権を取り直し、21 step動かせた。
- コード欄へfocusを置いた状態でもEscで停止した。
- 画面内のVLA/LLMは試験入力のまま。実モデル・実ゲームパッドの試験ではない。
- 自動操作はTEST_OPERATORと記録し、人の教師データにしていない。

テスト：

```bash
node --test tests/web/hybrid-keyboard.test.mjs
CAPX_LIBERO_SOURCE=capx/third_party/LIBERO-PRO \
  .local/openvla-sim/bin/python -m pytest \
  tests/test_vla_extension.py tests/test_hybrid_control.py -q --disable-warnings
```

JavaScript 7件、Python 37件がpass。初期化／記録／物理動作は実LIBERO-PROを含む。
