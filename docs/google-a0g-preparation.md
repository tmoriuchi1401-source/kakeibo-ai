# A0-G: 固定2ファイル・6 metadata GETの実施準備

基点はDraft #162の `3e7a5b39e6346f94edbf3caff477221710d4db8a`。
今回の変更は契約照合・HTTP送信制限・資格情報なし合成検証まで。
本番4処理・R1・無人runnerはNO-GO。#162や#163のhead、Apps Script、既存金融gateは変更しない。

## A0-Cと契約の差分

元のA0-G私的契約はclient未作成時のsnapshotで、`oauth_client_id=null`だった。
A0-Cの完了証跡にある専用Desktop clientを新しいprivate契約へ反映する。
client種別Desktop、External/Testing、本人1名、drive.file単独を照合する。
Consoleの設定を、実認可結果のscope/clientや到達性の証明にはしない。

private契約は `google_a0g.validate_contract` で検証する。既存の
`google_runner_preflight.validate_manifest` は変更せず再利用し、元のowner・固定book/親/sheet・
合成SYN-G101原本/親/MIME・将来の明示Firestore候補を維持する。
最初のA0-GはbookとPNGの正確な2 IDのみ。checkpoint slotやfolderをallowlistへ追加しない。
full runnerで必要な最低8slot・実効IAM・CAS/保存復元の条件も緩めない。

実IDは `.private/a0-g/contract.json` にのみ保存する（Git追跡禁止）。
`approved=false` と `storage=null` を保持し、別の将来計画に保存先候補と未発行IDを記録する。
owner permissionId、原本hash/version、book versionは未取得ならnullのまま。
過去connectorの正規化profile ID・modified time・名前から推測して埋めない。
初回metadataの観測値はprivateレビュー待ちで、契約を自動更新しない。

candidate SHAは実行するレビュー済みcommitを別のtrusted入力と照合する。
承認ハンドオフはSHA・private契約digest・期限に結び付け、契約を変えれば再レビューする。
client IDの追加がA0-G承認に変わる経路はない。

## 実装したデータ経路

`run_metadata_audit` は既定DISABLED。認可済み結果から渡された専用client・
drive.file単独scope・有効期限付きaccess tokenと、別工程の承認ハンドオフを明示注入する。
環境変数、ADC、metadata server、connector、本番Secretsから資格情報を探さない。
`ApprovedAttempt` は呼出元の承認システムが作るデータであり、人間の承認を証明する認証機構ではない。
Python内の任意コードを隔離するsandboxでもない。実行する固定SHAのcompositionレビューが必要。

`ReadOnlyPreflight` を、限定SDK形状→`GuardedMetadataHTTP`→literal GETのwire primitiveへ接続した。
単なるfake SDK契約ではなく、送信直前にmethod・URL・順序・全query・body/headerなしを検査する。
認証headerは内部で生成し、tokenをURL・ログ・metadata戻り値・ファイルへ出さない。

| 順序 | exact対象 | 取得範囲 |
|---|---|---|
| 1 | Drive about | 本人permissionId/email、quota metadata |
| 2 | 固定book files.get | owner・親・MIME・版・capabilities metadata |
| 3 | 同book permissions.list | owner-only ACL。追加ページは停止 |
| 4 | 同book Sheets get | sheet ID/title、includeGridData=false |
| 5 | 固定合成PNG files.get | owner・親・MIME・hash/version metadata |
| 6 | 同PNG permissions.list | owner-only ACL。追加ページは停止 |

POST/PATCH/PUT/DELETE、対象外ID、media/本文、検索、batch、任意discovery、redirect、
fields/queryの増減、追加scope、caller認証headerを送信前に拒否する。
順序と回数を固定し最大6request。各HTTPは最大10秒、全processは最大180秒、retry=0。
HTTP workerをparent watchdogで停止し、DNS/TLS/socket複数readを含む待機も制限する。
responseは各1MiBまで、重複JSON key・非有限値・不正metadataを拒否する。
401/403/404/429/5xx、redirect、通信断、token/承認期限切れはセッション停止。
自動refresh・再認可・scope拡大・共有変更・同じ要求の再送は行わない。
最初のaboutでowner不一致なら残りは送らない。pin済みbook版不一致は2件目で停止する。

成功も `METADATA_VERIFIED_NO_GO` / `runner_write_allowed=false`。
owner初回観測、source pin、book版、storage、実効IAM、保存試験などのblockerは残す。
返すmetadata全体はprivate用途であり、公開CI・PRへ出してはいけない。

## 認可・token寿命・失効

Google公式仕様を2026-10-25に確認した。

- Desktop/Mobile Pickerはdrive.file単独。PKCE S256、state、loopbackと固定2fileを使う。
  `authorization_parameters` は純粋なパラメータ生成のみで、ブラウザーを開かない。
  future callerはstate/verifierを暗号学的に生成し、code verifierをprivate memoryに保持する。
  callbackはstate・picked_file_idsの完全な集合一致・scope・拒否errorを検査する。
- access tokenの期限は交換responseの `expires_in` に従う。1時間を定数として仮定しない。
  `accept_exchange_result` は将来の承認済み交換結果を純粋検査するだけで、POSTしない。
- 認証POSTは別モジュール `google_a0g_oauth.ApprovedCodeExchange` に分離した。
  既定OFFで、SHA/契約digest/期限付きの認可・交換・refresh発行の各承認が必要。
  固定OAuth token endpointへのauthorization_code交換だけを1回許可する。
  callback/state/file/scope/PKCEを照合し、明示されたclient secret/codeはmemoryのみ。
  親watchdog、固定timeout、結果不明を含む再送禁止を使う。redirect/refresh/revokeは扱わない。
  合成senderで検証したのみで、実OAuth認可・実交換・token取得は行っていない。
- External/Testingでdrive.fileを使うrefresh tokenは通常7日。期限前の失効もあり得る。
  installed appではrefresh tokenが返る前提で、その発行条件も別承認へ含める。
  GET層にはaccess tokenだけを渡し、refresh tokenは受け渡し・保存・利用しない。
- 失効/invalid_grant/拒否時は停止。再認可は別の明示承認と新しい期限付きattemptが必要。
  Googleの失効はproject内の他client/token/scopeにも影響し得るため、専用projectの依存を確認し、
  account側での撤回かrevoke POSTを別承認する。tokenローカル削除だけを失効と扱わない。
  revoke操作はGET transportに混ぜず、今回のコードには実装しない。

公式: [OAuth有効期限](https://developers.google.com/identity/protocols/oauth2#expiration)、
[Desktop OAuth / token exchange / revocation](https://developers.google.com/identity/protocols/oauth2/native-app)、
[Desktop/Mobile Picker](https://developers.google.com/workspace/drive/picker/guides/desktop-mobile-picker)。

## 検証と成立条件の対応

| 既存条件 | 今回の結果 | 残る証拠・承認 |
|---|---|---|
| A0-C専用clientのprivate pin | 保存済み証跡との契約照合が成立 | 実施直前の同一client/本人/設定確認。実認可ではない |
| 固定2file・親・sheet・単独scope | 保存済み契約との一致、合成検証済み | 専用clientによる到達性/ACL fresh観測は未実施 |
| 実HTTP拒否transport | 合成で送信前拒否・timeout・停止を検証 | 実Googleのネットワーク/認証成功は未証明 |
| owner permissionIdとraw版 | 初回観測をレビュー待ちにできる | A0-G許可後のmetadata観測。自動pin禁止 |
| checkpoint/Firestore | A0のallowlistに含めずNO-GO維持 | 資源作成、IAM実効隔離、実保存復元は別工程 |
| OAuth認可・token取得 | plan/callback/resultと分離した交換adapterを合成検証 | 実ブラウザー本人認可・loopback受領の手動手順、秘密値hand-offの最終レビューと個別承認 |
| 実metadata GET | disabled、別承認と期限付きhandoffが必要 | SHA/契約digest/本人/正確な2file/期限を含む承認 |
| Secrets・無人runner | 未接続 | 専用Secrets/WIF/実port、固定workflow登録・手動1回・復元を段階承認 |

テストは完全合成credential/metadataを使用し、全live socketをpytestで拒否する。
CIも外向き通信不可namespace内で新テストと既存runner/preflightを実行する。
実Googleのfile GET・token発行・Secrets/IAM/Firestore変更はテストに含まれない。

## 工程別の承認

1. **認可・token発行**: 専用client、drive.fileの編集能力、正確な2file grant、本人、
   PKCE/state/loopback、refresh token発行可能性、memory-only取扱いと期限を提示して承認。
   auth adapterは準備済みだが実認可/交換は未実施。ブラウザーやlocal serverを自動起動するCLIは追加しない。
   新規承認と、review済みの本人認可/loopback受領手順を確認してから明示handoffする。
2. **6 metadata GET**: client/scope/本人/選択ID/期限を照合し、固定SHA・契約digestと限定取得内容を承認。
   最初の本人不一致、拒否、不明結果で停止。成功でもNO-GO。
3. **観測値pin**: raw owner ID・source hash/version・book版をprivateでレビューし、契約更新を承認。
4. **IAM・保存先作成**: 本番到達不能・他writer排除、具体policy、document/folder/8slot/book copyを個別承認。
5. **永続化試験**: 合成専用write/CAS/暗号化保存/独立read-back/別環境復元、予算・期限・復旧担当を承認。
6. **Secrets・実port・実行登録**: 専用protected environmentとWIF等を別承認。
   固定SHAのmanual-only workflow登録は別PR/別承認、その後の一回実行も別承認。scheduleは有効化しない。

この準備作業の完了をA0-G実施許可とは扱わない。実認可・token発行・実GETは未承認のまま。
