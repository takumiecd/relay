# 分岐図・固定した経路選択の検証

2026-11-02。[結果JSON](branch-graph-20261102.json)。

## 画面での確認

元の経路113stepと、step 80から分かれた分岐2の92stepを再構築した。
元の出力先を保持し、再生した全stepの物理状態が完全一致した場合だけ新しいconsoleを開いた。

IABの実画面で以下を確認した。

1. 分岐図の分岐2・step 87の点をクリックし、停止した物理状態へ復元。
2. 途中の87stepを表示したまま「この経路を採用 · step 92まで」をクリック。
3. 現在位置87と、採用した経路・終点92が独立して表示される。
4. 保存された操作列92行が、共有区間80行と修正区間12行からなる。画像・観測の参照先がすべて存在する。
5. 点と点の間の線をクリックし、間引いていたstep 88へ移動できる。
6. 再読み込み後も採用先が変わらない。
7. 元の経路の末尾113を表示しても採用先は分岐2・92stepのまま。

緑の線と終点は固定した採用経路、青いHEADは表示・操作している位置。
このconsoleのVLA／LLMは試験provider。人による品質判定や実モデルの性能の証拠ではない。
再構築のsourceはCODE_REPLAYであり、元のsourceとprovenanceを別項目で保持する。

## 実simulatorでの試験

- 過去の物理状態を表示しながら別のbranchを採用しても、物理状態・現在位置は変わらない。
- 親のprefixと修正suffixをつなぎ、画像・状態・指示・action・出所を対応付けた候補を出力する。
- 採用後のstep追加でも終点・操作列のbyte列が不変。
- 採用し直すと新しい選択IDになり、以前の選択ファイルを保存する。
- 古いcommand sequence・無効な終点は採用先を変更しない。
- 再起動で全branch・現在位置・採用先を復元し、分岐先のintegration stateと画像も一致する。

JavaScriptでは入れ子の分岐・共通祖先上のHEAD・線のクリック座標・長い経路の描画点数制限を検査する。
孫branchで親branchの固有区間より前へ戻って分岐した場合も、共有する祖先の行からつなぐ。

Python 44 passed、JavaScript 12 passed。構文確認と`git diff --check`も確認した。

## 保存と範囲

`history/manifest.json`から最新の採用先を参照する。
`history/adoptions/<id>/selection.json`・`route.jsonl`・`vla-candidates.jsonl`を保存する。
選択結果は`selected_for_dataset=true`、`training_admitted=false`。
任意の途中区間の採用UI・タスク成功／品質の判断・LeRobot/RLDS変換・学習投入は含まない。
