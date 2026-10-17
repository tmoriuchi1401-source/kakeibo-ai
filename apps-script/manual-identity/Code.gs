/** Deploy as USER_ACCESSING with Google-account access. Use the existing
 * manual-entry/Index.html unchanged. No Sheets, Drive or GitHub credentials. */
function identityActor_() {
  const allowed=JSON.parse(PropertiesService.getScriptProperties().getProperty('MANUAL_HOUSEHOLD_EMAILS') || '[]');
  const email=Session.getEffectiveUser().getEmail().toLowerCase();
  if (!Array.isArray(allowed) || allowed.length!==2 || !email || !allowed.includes(email)) {
    throw new Error('この家計簿へのアクセス権がありません');
  }
  return email;
}
function identityRequest_(action,fields) {
  identityActor_();
  const url=PropertiesService.getScriptProperties().getProperty('MANUAL_BACKEND_URL');
  if (!/^https:\/\/script\.google\.com\/macros\/s\/[A-Za-z0-9_-]+\/exec$/.test(url || '')) throw new Error('接続先の設定が必要です');
  const token=ScriptApp.getIdentityToken();
  if (!token) throw new Error('Googleの本人確認を許可してください');
  let result;
  try {
    const response=UrlFetchApp.fetch(url,{method:'post',contentType:'application/json',muteHttpExceptions:true,
      payload:JSON.stringify(Object.assign({},fields,{action:action,identityToken:token}))});
    if (response.getResponseCode()!==200) throw new Error();
    result=JSON.parse(response.getContentText());
  } catch (_) { throw new Error('接続できませんでした。登録を再送せず、受付状態を確認してください'); }
  if (!result || result.ok!==true || !result.result) throw new Error('本人確認または入力内容を確認してください');
  return result.result;
}
function doGet() {
  identityActor_();
  return HtmlService.createHtmlOutputFromFile('Index').setTitle('家計簿 手入力');
}
function manualBootstrap() { return identityRequest_('bootstrap',{}); }
function manualSubmit(input) { return identityRequest_('submit',{input:input}); }
function manualCancel(targetId,cancelId) { return identityRequest_('cancel',{targetId:targetId,cancelId:cancelId}); }
function manualStatus(id) { return identityRequest_('status',{id:id}); }

/** Owner runs once to obtain only the public OAuth audience for backend pinning.
 * The identity token itself never leaves the server or appears in logs/UI. */
function inspectIdentityAudience() {
  identityActor_();
  if (Session.getEffectiveUser().getEmail().toLowerCase()!=='tmoriuchi1401@gmail.com') throw new Error('管理者のみ利用できます');
  const token=ScriptApp.getIdentityToken();
  if (!token) throw new Error('本人確認を許可してください');
  const claims=JSON.parse(Utilities.newBlob(Utilities.base64DecodeWebSafe(token.split('.')[1])).getDataAsString());
  if (typeof claims.aud!=='string' || !claims.aud) throw new Error('本人確認を許可してください');
  PropertiesService.getScriptProperties().setProperty('MANUAL_IDENTITY_AUDIENCE',claims.aud);
  console.log('OAuth audience: '+claims.aud);
  return claims.aud;
}
