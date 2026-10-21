# 確認1タブと合成adapterの接続

基点は #155 最終head `985fddb87f1872549f9b33cc88ce97e8e973ca25`。
既存 `iphone_one_tab.html` の1案件1行・列配置・固定案件ID/見出し・横スクロールを再利用。
新DB、金融writer、別の確認サービス、実Google transport、Cloud構成は追加しない。
main・#153〜#155・正式Sheets・歴史的4タブの変更はない。

## 到達範囲

既存の合成RSA/OAuth/session/CSRF hostに #155 のルートだけを組み合わせる。
`/isolated-synthetic/iphone-sheet-handoff` は既存画面の合成adapterモード。
既存R1の金融POSTルートはこのcompositionに登録しない。

1. **最新の下書きを取り込む**：明示POST prepare → POST request（confirmed=false）。
   #155 RawReader二重読取り・独立原本照合・journalと既存draft CASを通す。
2. **取込内容を比較**：別の明示POST prepare（書込み0）。案件ID、種類、原本ID/hash/revision/native版、
   Sheet snapshot、要求ID、修正前後、変更項目、明細カテゴリ/状態/メモ、検証した全値を表示。
3. **本人確認を確定（記帳しない）**：比較表示後のチェックと別ボタン。
   #155の同じproof・case・snapshot、confirmed=trueだけをPOST。資格付与・authority・writerを呼ばない。
4. **同じ要求の結果を照会**：認証済み保存結果GETのみ。`FINANCIAL_BOUNDARY_STOPPED` は
   **確認済み・未記帳**。記帳完了とは表示しない。

入力の編集元は正式「確認」1タブのまま。このモードの欄は**保存済み取込内容の読取専用表示**であり、
ブラウザー入力の保存APIは新設していない。取込前は既存backend下書き、取込後はCASで保存された内容を表示する。
実Sheetから読むtransportは未接続。自動試験はメモリ上の合成Sheetを変更して入力編集を再現する。
既存R1モードの編集・保存機能は維持する。行番号・注記・Sheet状態を本人承認に使わない。

## 状態と回復

| 表示 | 意味・許可される操作 |
|---|---|
| 未確定・未取込 | 取込。比較・確定は不可 |
| 下書き取込済み・未確定 | 比較。新たな入力は明示再取込後に再比較 |
| 保留 | 必須不足、明細別入力不足、対応不明。入力を保持し確定不可 |
| 競合 | 入力・原本・版・journalの変化、重複等を拒否。送信後の受付状態は結果GETで照合 |
| 期限切れ／認証拒否 | 入力を保持。再認証・画面再読込み。古い比較を再利用しない |
| UNKNOWN | 結果照会だけ。自動再送・新しい要求による再確定なし |
| 確認済み・未記帳 | 金融境界で停止。結果照会だけ |

全行を操作中ロックし、二重タップを抑止する。POST前にownerで分離したsessionStorageへ案件/要求IDだけを記録する。
proof、金額、医療入力、メモ、認証tokenを保存しない。要求の応答が不明なら全行を停止する。
HTTP拒否でもdurable ATTEMPTINGが残り得るため要求IDを消さない。明示結果GETでterminalを観測したときだけ
同じ要求IDのブラウザー側ヒントを解除する。UNKNOWNのGETは解除しない。
prepareの通信失敗も不明として停止する（書込みは0）。要求IDを取得できなかった場合はUNKNOWNのまま照合待ち。
sessionStorageが使えない場合はfail-closed。

再読込み・戻る/bfcache復帰・再認証で比較proofとチェックを破棄する。GETだけでCASやfresh Sheet観測は実行しない。
サーバーjournalのATTEMPTINGはプロセス再構築後も全行を停止する。
ブラウザーヒントはauthorityではなく、消去・別端末でもサーバーのCAS/競合/重複拒否は有効。
サーバー未到達の要求IDを別端末へ永続同期する新DBは設けていない。

一般は既存の明細ID・個別カテゴリ・入力状態を保持し推測配賦しない。医療は完全手入力・途中保存を維持し、AI経路なし。
G103/H301・旧G102/M102・PDFと既存旧医療p1を保留表示。原本bytes収録済みの一般/医療画像だけプレビューする。
保留原本リンクは「合成原本未収録」の表示に留め、PDF構造を確定したように扱わない。

## 自動検証と隔離

新UIのhost・Chromium全試験は実際の既存認証とFlaskハンドラーを使う。
ブラウザーの全要求を合成originだけに制限しプロセス内へ渡す。Google/Firestore client構築をfail-on-call、
Python socket接続を拒否し、金融writer・native/shared/ledger/authorityの書込みをfail-on-callで遮断する。
新UI経路のwriter呼出し0、既存金融port前後不変、UNKNOWN自動再実行0を検証する。

Ubuntu CIは依存導入後 `unshare --net` + 非root実行でChromium/pytestをloopbackだけの名前空間へ隔離。
外部interface/IPv4・IPv6の外部向けroute 0、親namespaceとの差とChromiumの各process所属を検証する。
ページ要求は全てin-processであり、外部接続用のHTTP listenerも公開しない。
Windows試験はローカルsocketpairを許可し、全ページ要求のinterceptと到達不能localhost proxy/DNS拒否で遮断する。
WindowsをOSネットワーク名前空間による隔離の証拠とは扱わない。

1365px / 390px：一般・医療の取込→比較→未チェック拒否→明示確認→結果、二重タップ、固定ID、本文幅、
原本popup往復、入力/メモ保持、保留、通信未到達/応答消失/ATTEMPTING、再読込み/戻る、競合/期限切れ/重複、
再認証での比較破棄を対象にする。画像と要求一覧・境界counter・namespace証拠・JUnitをCI artifactへ保存。
これはiPhone実機合格の証拠ではない。

## iPhone実機での確認方法（未実施）

このPRは到達可能なhostを公開しない。次工程で別承認の隔離端末接続方法を決め、合成hostと合成入力だけを使って行う。
本番認証や実Sheetsへ接続する手順ではない。

1. 縦向きでG101を検索。横へ動かしてもIDと見出しが分かり、誤タップせず44px以上のボタンを押せるか確認。
2. 合成原本を開き、元画面に戻って案件・検索・保存下書きが保持されるか確認。
3. 最新下書き取込後に別操作で比較。ID・原本・日付・金額・カテゴリ・差分を読む。
   チェック前は確定不可、チェック後の別ボタンで「確認済み・未記帳」、結果照会も同じIDになるか確認。
4. 医療の途中入力とメモ、G103/H301/PDF保留を確認。読み取り欄を編集できるように見誤らないか確認。
5. 合成host側の応答消失を再現しUNKNOWNを表示。戻る・再読込み・再認証後も再送不可で結果照会だけになるか確認。
6. 画面回転、ピンチ縮小、Safari復帰/終了再開、別タブ・原本往復で表示と入力対応を確認。
   Sheetsアプリの倍率/キーボード/同期維持は既存の実機未確認項目として別記録する。

実機の機種/iOS・アプリ・倍率・操作と結果を記録するまで未確認のままにする。

## 次の工程と承認

- まず本DraftのコードレビューとiPhone実機受入。隔離hostへ端末を接続する方式の決定・公開は別工程。
- 実Sheet/Drive/native transport、backend CAS/ACL/鍵/耐久性、複数端末の不明要求照合、旧writer競合排除は未実装/未検証。
- 実入力を画面から直接編集する要求がある場合は正式1タブ編集との競合・保持仕様を先に確定する。
- 本番接続、金融canary/authority、merge/deployは別承認。この実装は実金融writeの直前で止める。
- 従来4処理・新R1ともNO-GOを維持。Variables/Secrets/validated SHA、原本移動、AI送信、Cloud/IAMの変更なし。

ローカル最終検証：adapter/host/表示110、PC/390px Chromium34、既存回帰332、計476 passed。
syntax/compile/whitespace成功。UbuntuのOS隔離結果は本PRのCIとartifactで確認する。
