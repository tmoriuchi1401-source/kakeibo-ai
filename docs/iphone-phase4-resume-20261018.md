# Phase 4A～4B 再開検証（2026-10-18）

対象はDraft #128と既存のPhase 4専用合成ブックだけ。main統合、本番接続、実POST、Cloud、
会計write、原本移動、AI送信、実authority更新は行わない。

## 復旧前の確認と現在の制約

- 不正追加行は **4行目／C4**。前回報告のC5は記載誤り。停止時raw snapshotのindex=3とも一致する。
- 現在のA1:Q1001全体をCellDataで読み、値・数式・注記・入力規則・format/chip metadataを
  Git除外の`.private/phase4-r2-full-cells-before.json`へ保存した。
- 停止時snapshotと現在の8案件のA:Qは全て一致。保存済みjournalの8件とも一致。
  追加行はG103の原本リンク数式だけで、他の値・注記・未送信の下書きはない。
- **保護設定の現在値を完全退避するまで削除しない。** 通常のmetadata読取りツールは
  protectedRanges/tablesを省略する。既存の保護snapshotを現在値として流用しない。
- 同一タイトルを指定するmetadata read-back probeは自動承認レビューが書込みとして拒否。
  再試行せず、追加承認を依頼した。接続済みdocument-control sessionも0件。
  この時点では削除・sort・表設定変更を実施していない。

## 自動補完のread-only調査

API保存済みTableは`SyntheticReviewCases`、A:Qを含むnative Table。
列設定はcolumnNameだけで、原本列に明示的な計算列式は公開metadata上存在しない。
原本列のセルにはそれぞれ個別のHYPERLINK数式があり、ARRAYFORMULAではない。
前回のinsertDimension（inheritFromBefore=false）だけで、挿入位置の次案件G103の式が追加行へ入った。
値・注記は複製されず、規則は継承された。これはSheets側の行挿入時の数式継承という観測であり、
Table内部の計算列実装まで特定したという主張ではない。

ブラウザでTableメニューと原本列メニューを読取り確認したが、式の自動展開を切る設定はない。
Toolsの予測入力・数式候補は有効だったが、候補表示とAPI行挿入時の実数式コピーは別挙動。
その設定の影響が専用ブックだけに限定される証拠がなく、変更していない。
Table削除や大規模列変更は行わない。

公開APIの[TableColumnProperties](https://developers.google.com/workspace/sheets/api/reference/rest/v4/spreadsheets/sheets#TableColumnProperties)
には計算列数式／自動補完停止のfieldがない。
[Googleの予測入力設定説明](https://support.google.com/docs/answer/75509)も、API挿入時の継承を停止する保証ではない。
後続の独立baselineでinside/outside挿入等を実測するまで、Tableだけが原因と断定しない。

## adapterの変更

- 原本リンクを管理側の案件ID・kind・原本IDから生成し、その完全なformulaValueを比較。
  リンクセルだけを案件として認識しない。原本IDの文字種も検証する。
- 8案件集合・目的別kind・原本identity・保護・注記・規則・表範囲・順序をfail-closedで照合。
  検証用の有限グリッド全体（1000×26、30,000セル未満）をGET専用で読み、遠い空行の
  IDなし数式や下書きを小さいsentinel窓の外へ見落とさない。任意の巨大本番gridは拒否。
- journalは入力セルとcase/kind/source identity、最新receiptとbatch、revision連鎖を再検証。
  `save_current`は新しいGET結果と呼出し側の期待Sheet digestを比較し、CASと別にstaleを拒否。
  過去requestのreplay/recoveryは、現在の下書きかどうかを`is_current`で区別する。
- 一般・医療・PDFは既存`iphone_review_preview`とnative validatorを再利用。
  Sheetの表示IDと既存のidentity由来Case IDを、管理側`SheetNativeBinding`で明示対応する。
  native Case IDをSYN名へ書き換えたり、page/Unit/authorityを捏造したりしない。
- `VersionBinding`はDrive revision IDと既存native source versionを別fieldとして保持する。
  管理側のfresh readerが両方を2回co-observeして一致した場合のみbindingを作る。
  文字列が同じというだけの推定、数値versionへの変換、Sheetセル由来のaliasは採用しない。
  実native hostのfresh version readerは未接続のまま。
- Sheets編集・読込み成功・journal保存を確定要求へ変換する処理、実POST受付はない。

## 合成検証とCI

Windowsローカルでは新reader／journalとnative testのうち113件PASS。
PDF構造の1件は既存のhash-pinned native fixtureがWindowsで`bundle_result_changed`となるため、
Linux CIで検証する。pinや検証条件は緩和していない。
最初のnative fixture実行はsandbox既定tmpへの権限不足もあり、以後Git除外のworkspace tmpを使用。

追加テストはstale Sheet、CAS競合、同じrequestのpayload変更、応答不明の前後、
子processの強制終了・再起動後のread-only復旧、OS lock解放、atomic replace失敗、
journal破損と未送信入力保持を含む。専用local JSONのみ使用し、DB/サービスは追加しない。
子processにはGoogle/Cloud/application credentialsを渡さない。

一般・医療・PDFのnative dry-runは合成native fixture上で、既存writer/submit/saveを禁止して検証する。
PDF構造合格は会計許可にしない。一般の単一カテゴリを複数明細へbroadcastしない。
実ブックの8案件にnative Unit/登録済み明細が存在すると推定しない。

専用Linux CIを追加：PR baseは`codex/iphone-one-tab-safety`だけ、contents:read、
write enabled=false、秘密情報・environment・Cloud・artifact uploadなし。
既存purpose/owner/session/CSRF/duplicate/replay/revision/read-back/hold契約も回帰実行する。
CI結果は実行後に追記する。

## 残作業・承認

保護設定の退避経路が使えるようになった後に、承認済み4行目削除の条件を再確認する。
削除後の8案件・40入力slot（G103保留6を含む）・7原本・注記・規則・保護・表範囲を照合し、
独立baselineから構造変更negative testを行う。予測できた補完は検出・拒否・承認範囲での復旧を
試験し、同じ既知現象だけで開発を無期限停止しない。

通常操作は検索のまま。ownerの直接編集・sortを完全禁止できるとは扱わない。
本番接続の前には、実ホストのcanonical native reader・版対応・追加ユーザー認可・明示POST・
既存writer gate整合・iPhone実機再確認が必要で、それぞれ別の承認対象である。
