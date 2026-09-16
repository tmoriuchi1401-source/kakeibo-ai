"""Temporary loopback UI for the owner; originals are never AI/tool output.

Read-only Drive access. Explicit human review writes a local instruction file,
not Drive, Sheets, accounting, or a send intent. Publish under maintenance later.
"""
import argparse
from hashlib import sha256
from http.server import BaseHTTPRequestHandler, HTTPServer
import base64
import json
import os
from pathlib import Path
import secrets
import time

from .medical_anonymization import LABELS, png, render_single_page
from .medical_crop_review import make_review, selected_crop, identity_key

PAGE='''<!doctype html><html lang="ja"><meta charset="utf-8"><title>支払額画像の匿名化確認</title>
<style>body{font:16px sans-serif;max-width:1050px;margin:25px auto;padding:0 20px;color:#223}
canvas{max-width:100%;border:1px solid #999;touch-action:none;cursor:crosshair}
button,select{font:inherit;padding:9px;margin:8px 4px}#result{white-space:pre-wrap}#crop{max-width:100%;border:2px solid #468}
.note{padding:12px;background:#fff3d7}label{display:block;margin:14px 0}</style>
<h1>支払額画像の匿名化確認</h1>
<p>この端末内だけの確認画面です。原本をAIに送信しません。金額の手入力・記帳確定は行いません。</p>
<p>原本上で<strong>実支払額とその印字ラベルだけ</strong>を囲んでください。ドラッグし直して範囲を修正できます。
この1枚・この版だけに適用され、他の帳票へ流用しません。</p>
<canvas id="page"></canvas>
<p>印字ラベル <select id="label"><option value="">選んでください</option>__LABELS__</select>
<button id="preview">切出しを確認</button></p>
<section id="check" hidden><h2>AIへ送る予定の画像</h2><img id="crop" alt="選択範囲の新しいPNG">
<p class="note">送信してよいのは支払額・ラベル・必要な罫線だけです。
氏名、患者/保険番号、住所/電話、生年月日、署名、QR/バーコード、施設名、日付、診療内容が
少しでも残る場合は保存せず、範囲を修正してください。安全に分けられない場合は確認シートへ手入力してください。</p>
<label><input type="checkbox" id="confirmed">上の最終画像を自分で確認しました。実支払額と選択した印字ラベルだけを含み、個人・機密・医療情報は含みません。</label>
<button id="save" disabled>この画像の確認結果を保存</button>
<p>保存だけではAI送信も会計確定も行いません。利用プラン確認・本番での同一画像検証後に解析します。</p></section>
<p id="result" role="status"></p>
<script>
const el=id=>document.getElementById(id),c=el('page'),ctx=c.getContext('2d'),img=new Image();
let start=null,box=null,ticket=null;
const redraw=()=>{ctx.drawImage(img,0,0);if(box){ctx.strokeStyle='#0068dc';ctx.lineWidth=3;ctx.strokeRect(box[0],box[1],box[2]-box[0],box[3]-box[1]);}};
const reset=()=>{ticket=null;el('check').hidden=true;el('confirmed').checked=false;el('save').disabled=true;};
const point=e=>{let r=c.getBoundingClientRect();return [Math.max(0,Math.min(c.width,Math.round((e.clientX-r.left)*c.width/r.width))),Math.max(0,Math.min(c.height,Math.round((e.clientY-r.top)*c.height/r.height)))];};
img.onload=()=>{c.width=img.width;c.height=img.height;redraw();};img.src=location.pathname+'/page.png';
c.onpointerdown=e=>{reset();start=point(e);c.setPointerCapture(e.pointerId);};
c.onpointermove=e=>{if(!start)return;let p=point(e);box=[Math.min(start[0],p[0]),Math.min(start[1],p[1]),Math.max(start[0],p[0]),Math.max(start[1],p[1])];redraw();};
c.onpointerup=e=>{start=null;};el('label').onchange=reset;
async function post(action,data){let r=await fetch(location.pathname+'/'+action,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(data)});if(!r.ok)throw new Error('保存・検証できませんでした。範囲とラベルを確認してください。');return r.json();}
el('preview').onclick=async()=>{try{reset();let r=await post('preview',{coordinates:box,label:el('label').value});ticket=r.ticket;el('crop').src='data:image/png;base64,'+r.png;el('check').hidden=false;el('result').textContent='最終画像を確認してください。';}catch(e){el('result').textContent=e.message;}};
el('confirmed').onchange=()=>{el('save').disabled=!el('confirmed').checked||!ticket;};
el('save').onclick=async()=>{el('save').disabled=true;try{await post('confirm',{ticket:ticket,confirmed:el('confirmed').checked});el('result').textContent='確認結果を非公開のローカルファイルへ保存しました。AI送信・Drive更新・記帳は0件です。この画面を閉じて構いません。';ticket=null;}catch(e){el('result').textContent=e.message;}};
</script></html>'''


class ReviewSession:
    def __init__(self,source,image,key,output,verify):
        self.source=source;self.image=image;self.key=key;self.output=Path(output);self.verify=verify
        self.pending=None;self.saved=False

    def preview(self,value):
        if self.saved or set(value)!={'coordinates','label'} or value['label'] not in LABELS:
            raise ValueError('review_invalid')
        data=selected_crop(self.image,value['coordinates'])
        l,t,r,b=value['coordinates']
        if (r-l)*(b-t)>self.image.width*self.image.height/3:
            raise ValueError('payment_region_must_be_minimal')
        self.pending=dict(value,ticket=secrets.token_urlsafe(32))
        return {'ticket':self.pending['ticket'],'png':base64.b64encode(data).decode()}

    def confirm(self,value):
        if (self.saved or not self.pending or set(value)!={'ticket','confirmed'}
                or value['confirmed'] is not True or value['ticket']!=self.pending['ticket']):
            raise ValueError('current_preview_confirmation_required')
        self.verify()
        record=make_review(self.source,self.image,self.pending['coordinates'],self.pending['label'],
            confirmed=True,key=self.key)
        # Exclusive: never overwrite an earlier human decision on a retry.
        with self.output.open('x',encoding='utf-8') as handle:json.dump(record,handle,ensure_ascii=True)
        self.saved=True;self.pending=None
        return {'saved':True}


def handler(session,token,origin):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self,*args):pass
        def reply(self,code,payload,mime='application/json'):
            self.send_response(code);self.send_header('Content-Type',mime)
            self.send_header('Cache-Control','no-store');self.send_header('Referrer-Policy','no-referrer')
            self.send_header('X-Content-Type-Options','nosniff')
            self.send_header('Content-Security-Policy',"default-src 'none'; img-src 'self' data:; style-src 'unsafe-inline'; script-src 'unsafe-inline'; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'none'")
            self.send_header('Content-Length',str(len(payload)));self.end_headers();self.wfile.write(payload)
        def valid(self):
            return self.headers.get('Host')==origin.removeprefix('http://')
        def do_GET(self):
            if not self.valid():return self.reply(403,b'{}')
            if self.path=='/'+token:
                labels=''.join('<option>'+v+'</option>' for v in LABELS)
                return self.reply(200,PAGE.replace('__LABELS__',labels).encode(),'text/html; charset=utf-8')
            if self.path=='/'+token+'/page.png':return self.reply(200,png(session.image),'image/png')
            self.reply(404,b'{}')
        def do_POST(self):
            if not self.valid() or self.headers.get('Origin')!=origin:
                return self.reply(403,b'{}')
            try:
                size=int(self.headers.get('Content-Length','0'))
                if not 0<size<=4096 or self.headers.get('Content-Type')!='application/json':raise ValueError()
                value=json.loads(self.rfile.read(size))
                if self.path=='/'+token+'/preview':result=session.preview(value)
                elif self.path=='/'+token+'/confirm':result=session.confirm(value)
                else:return self.reply(404,b'{}')
                self.reply(200,json.dumps(result).encode())
            except Exception:self.reply(400,b'{"error":"review_not_saved"}')
    return Handler


def load_session(binding_path,output,review_id=None):
    from .google_clients import read_only_drive_service,download_drive_file
    from .drive_run_state import DriveStateTransport
    from .receipt_reimport_production import ResultBinding,ReimportStore
    from .settings import Settings,service_account_source
    if os.environ.get('GEMINI_API_KEY'):raise ValueError('review_must_not_receive_ai_key')
    output=Path(output).resolve()
    if output.is_relative_to(Path(__file__).resolve().parents[1]) or output.exists():
        raise ValueError('review_output_must_be_new_private_file')
    binding=json.loads(Path(binding_path).read_bytes());drive=read_only_drive_service()
    store=ReimportStore(DriveStateTransport(drive,ResultBinding(binding['parent_folder_id'],binding['file_id'])),
        binding['manifest_digest'],Settings().spreadsheet_id)
    items=[v for k,v in store.value['confirmation_items'].items()
        if v['kind']=='medical' and v['status']=='waiting' and (review_id is None or k==review_id)]
    if len(items)!=1:raise ValueError('select_exactly_one_waiting_medical')
    item=items[0];source=item['source']
    def verify():
        meta=drive.files().get(fileId=source['source_id'],fields='version,parents,trashed,mimeType').execute(num_retries=0)
        if (meta.get('trashed') or meta['version']!=source['version'] or meta['parents']!=[item['folder_id']]
                or meta['mimeType']!=source['mime_type']):raise ValueError('review_source_changed')
    verify();payload=download_drive_file(source['source_id'],drive);verify()
    if sha256(payload).hexdigest()!=source['sha256']:raise ValueError('review_source_changed')
    image=render_single_page(payload,source['mime_type'])
    path,info=service_account_source();info=info or json.loads(Path(path).read_bytes())
    return ReviewSession(source,image,identity_key(info['private_key']),output,verify)


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--binding',required=True);parser.add_argument('--output',required=True)
    parser.add_argument('--review-id');args=parser.parse_args()
    session=load_session(args.binding,args.output,args.review_id)
    token=secrets.token_urlsafe(32);server=HTTPServer(('127.0.0.1',0),BaseHTTPRequestHandler)
    origin='http://127.0.0.1:'+str(server.server_port)
    server.RequestHandlerClass=handler(session,token,origin);server.timeout=1
    with Path(args.output+'.url').open('x',encoding='utf-8') as handle:
        handle.write('[InternetShortcut]\nURL='+origin+'/'+token+'\n')
    # No URLs/IDs/originals/OCR in terminal logs; private .url is for the owner.
    print('Private review UI ready; see the local .url file.',flush=True)
    deadline=time.monotonic()+3600
    try:
        while time.monotonic()<deadline and not session.saved:server.handle_request()
    finally:server.server_close()


if __name__=='__main__':
    try:main()
    except Exception:print('Private review UI stopped; no automatic retry.');raise SystemExit(1)
