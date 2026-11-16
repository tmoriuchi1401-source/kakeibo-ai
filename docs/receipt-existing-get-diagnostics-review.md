既存GETだけを使うreceipt version診断（review専用）

基準SHA：`fe90f64b47ffefae1efbca0ae7f646002e50576f`。調査対象の過去run：`43787722650`。
過去runの原因を確定した変更ではない。実環境へ適用せず、本番NO-GOを維持する。

**変更の範囲**

`ReceiptPrivateAdapter`とreceipt専用`ExistingReceiptDriveIO`の既存応答処理地点に観測フックを置く。
共有`DriveRegistryIO`、workflow、受付、journalの遷移規則、Sheet/cache、実行設定は変更しない。
診断器はHTTPや永続化機能を持たず、専用transportだけが保持する。

既存GETのURL・fields・順序・回数、PUTのURL・payload・If-Match・回数は変更しない。
追加GET、retry、redirect、並列化、readback失敗後の補完取得は行わない。

**観測点と情報の限界**

次の番号は既存の合成claim-CAS故障fixtureのHTTP列である。実worker全体の通し番号ではない。

|点|位置|version|ETag|内部の時刻／順序|
|---|---|---|---|---|
|P|独立prewrite readの外側最終metadata（18）|既存応答にある|ある|応答処理完了時の単調時刻とordinal|
|C0/C1|CAS実装内のtransport metadata前後（21／23）|既存projectionにない|ある|同上|
|O0|CAS成功後のreadback外側metadata（26）|ある|ある|同上|
|T0|readback transport内metadata前（29）|既存projectionにない|ある|同上|
|B|readback本文GET（30）|対象外|本文応答からのETag採用はしない|コードの出力順でT0とT1の間に位置する|
|T1|readback transport内metadata後（31）|既存projectionにない|ある|応答処理完了時の単調時刻とordinal|
|O1|外側metadata最終取得（33）|ある|ある|同上|

初回loadは1～9、独立prewrite readは10～18、CAS内の既存再読取は19～23、PUTは24。
正常時のadapter readbackは25～33、coreの既存ACK readbackは34～42。
claim全体は41 GET＋1 PUTのまま。不一致なら元と同じ地点で短絡し、後続GETは実施されない。
観測は独立prewrite readからadapter readbackまでに限定し、core ACKの判定は変更しない。

既存の内側metadata fieldsは`id,etag,parents(id),labels(trashed),mimeType`であり、versionは含まれない。
HTTP要求自体も維持するため、内側versionの比較は`unavailable`と出す。ETagからversionを推測しない。
全観測点でversionを得るにはfields拡張という別の変更が必要で、この案では実施しない。

主な比較はP→O0（CAS境界）、O0→T0、T0→T1、O0→O1、T1→O1。
prewrite内とCAS内部の既存metadataの対応も、同じ固定コード方式で観測する。
versionは文字列一致と、20桁以内のASCII十進値による増加／減少を区別する。
異なる表記で数値だけが等しい場合も`representation_changed`とし、既存の文字列等値検証には影響させない。
ETagは一致／不一致だけを比較し、版の順序や増減を読み取らない。

メモリ内ではordinalと単調時刻を比較し、取得順と各応答処理完了地点間の経過時間を求める。
ログには`under_100ms`／`under_1s`／`under_5s`／`at_least_5s`の区分だけを出す。
これは各HTTP要求の正確なRTT、サーバー更新時刻、HTTP Dateの比較ではない。計測・ログ出力自体の時間も含む。

CAS予定本文は既に構成済みのバイト列からSHA-256を計算する。
Bの既存本文応答もSHA-256で比較し、`same`／`different`／`unavailable`のみを出す。
transport／外側metadataの検証でその後停止しても、取得済み本文の比較結果を残せる。
ただし未取得の本文、GET応答喪失、GETより後の外部更新は補完も推測もしない。

**ログと停止の境界**

新規ログは`receipt_get_diag_*`の閉じた許可集合のみ。Drive由来値をコード名へ連結しない。
各LogRecordにformat引数、exception、stack、extraを付けない。
生のversion、ID、ETag、hash値、本文、資格情報、数値時刻、任意の例外文を出さない。
比較用ETagとdigest、ordinal、時刻はメモリ内だけに置き、各条件付き書込みの終了時に消去する。
診断例外は既存の処理結果へ伝播させず、代替IOや再試行を行わない。
診断が一部欠落した場合は原因未確定とし、成功や再送許可の根拠にしない。

例：`receipt_get_diag_post_outer_pair_version_up`と
`receipt_get_diag_post_outer_pair_etag_same`は、readback外側前後でversionだけが増加したという観測である。
外部更新者やDrive不具合を確定するラベルではない。
`receipt_get_diag_cas_unacknowledged`は、CAS呼出しが正常復帰しなかったことだけを示す。
PUT未到達、サーバー書込み前の喪失、書込み後の応答喪失は、このラベルだけでは区別できない。

metadata全体の等値比較、version／ETag検証、If-Match、ACL・親フォルダ・catalog、
本文バイト等値、新ETag、ACK、`_stopped`の既存制御はそのまま残す。
SHA-256の一致は既存本文バイト等値検証の代替にならない。
診断コードは正常判定・不一致の無視・stop解除・追加CASに使用しない。

**オフライン検証**

関連既存テストは変更前189件が成功。変更後は同じ範囲と新規59件、計248件が成功した。
追加でSheet connection／decoder／validation／catalog binding／共有PDF registryの既存280件が成功し、
関連検証の合計は528件。ネットワークを禁止した合成fixtureのみで、実UUIDやprivate証拠をテストへ入力していない。

- 正常CASに伴う版更新：P→O0のversion増加とETag変化、readback内の安定、本文一致を区別。
- CAS後version反映遅延の観測モデル、version増加／減少、ETagのみ、両方の変化：元と同じ短絡地点で停止。
- 外部更新モデル：最初の外側metadata後に合成バックエンドを更新。本文不一致とETag差を観測して停止し、外側最終GETは増やさない。
- 本文不一致、ETag不変、GET応答喪失、PUT前／PUT後の応答喪失：CAS試行数、永続状態とUNKNOWN／再送禁止を維持。
- 全既存readback故障プロファイルについて、診断器を外した対照とHTTP要求・永続状態・停止状態を丸ごと比較。
- 診断の時計・hash計算・ログ出力が例外を起こす場合も、元の成功／失敗結果とHTTP列を維持。
- versionの不正値・巨大値・非ASCII、ID／ETag／本文の秘密値sentinelを入力し、閉じたコード以外やdigestがログへ出ないことを検査。

加えてローカルで基準SHAの実クラスと変更後クラスを84故障プロファイルで差分実行し、
HTTP列（URL・params・If-Match・PUT本文digestを含む）、CAS回数、永続本文／ETag、停止と照合後の再送禁止、
既存の停止ログ列が一致した。この差分検証ではネットワークを禁止した。
基準SHAの比較用ソースと実案件の証拠はreview branchへ同梱しない。

**取得できる証拠と残る未確定事項**

新たに取得できるのは、CAS境界とreadback内を分けたversion増減／ETag等値、
本文と予定値のSHA-256等値、既存応答の順序・粗い時間区分、到達しなかった後続観測点である。
versionのみの変化と本文・ETagも変わる状況を区別しやすくなる。

内側GETのversion、正確なサーバー時刻、外部更新者、metadata更新の完全な監査、
CASに伴う反映遅延と古いAPI観測の区別、応答喪失時に実際に書き込まれたかどうかは確定できない。
同じ観測列を複数の原因が生成できるため、比較結果だけで三者択一の原因確定をしない。
この変更を実行しない限り将来の観測も発生せず、過去runの不足証拠を遡って回収することもできない。

専用review branchへのpushのみを行い、PR作成・merge・実行branch切替・dispatch・有効化・実環境適用は行わない。
