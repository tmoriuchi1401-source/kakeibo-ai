"""Synthetic-only whole-entry dependencies, including in child processes."""
import json
import os
from urllib.parse import urlencode,parse_qs,urlsplit

from staging_shadow import google_a0g as a0
from staging_shadow import google_a0g_local as local
from staging_shadow.google_a0g_loopback import parse_callback
from test_google_a0g import Sender


def install(monkeypatch,root,counter,mode='normal'):
    captures={'receivers':[],'browsers':[],'grants':[],'authorization':[],'exchange_params':[]}
    def mark(kind):
        with open(counter,'a',encoding='utf8') as f: f.write(kind+'\n')
    class Browser:
        def __init__(self,executable): self.closed=False; captures['browsers'].append(self)
        def open(self,url):
            if mode=='crash-before-browser': os._exit(92)
            mark('browser')
            captures['authorization'].append(parse_qs(urlsplit(url).query))
        def close(self): self.closed=True
    class Receiver:
        redirect_uri='http://127.0.0.1:8765/oauth2callback'
        def __init__(self,**kw): self.kw=kw; self.closed=False; captures['receivers'].append(self)
        def receive(self):
            q=dict(state=self.kw['state'],code='SYNTHETIC_CODE',scope=a0.DRIVE_FILE,picked_file_ids='book,png')
            if mode=='callback-denied': q={'error':'access_denied'}
            if mode=='callback-timeout': raise TimeoutError('SYNTHETIC_CODE')
            return parse_callback('/oauth2callback?'+urlencode(q),contract=self.kw['contract'],expected_sha=self.kw['expected_sha'],state=self.kw['state'])
        def close(self): self.closed=True; self.kw={}
    def exchange(params,**kwargs):
        mark('exchange')
        captures['exchange_params'].append(dict(params))
        if mode=='crash-exchange': os._exit(93)
        if mode=='exchange-timeout': raise TimeoutError('SYNTHETIC_CODE SYNTHETIC_REFRESH')
        if mode=='interrupt': raise KeyboardInterrupt('SYNTHETIC_CODE')
        return a0.Response(200,json.dumps(dict(access_token='SYNTHETIC_ACCESS',refresh_token='SYNTHETIC_REFRESH',scope=a0.DRIVE_FILE,expires_in=90,token_type='Bearer')).encode())
    sender=Sender()
    def get(url,headers,**kwargs):
        mark('get')
        if mode=='crash-get': os._exit(94)
        if mode=='get-denied': return a0.Response(403,b'SYNTHETIC_ACCESS')
        return sender(url,headers,**kwargs)
    monkeypatch.setattr(local,'REPO',root)
    monkeypatch.setattr(local,'verify_checkout',lambda sha:None)
    monkeypatch.setattr(local,'PrivateBrowser',Browser)
    monkeypatch.setattr(local,'LoopbackReceiver',Receiver)
    monkeypatch.setattr(local.oauth,'token_post',exchange)
    monkeypatch.setattr(local.a0,'https_get',get)
    original=a0.accept_exchange_result
    def accept(*args,**kwargs):
        grant=original(*args,**kwargs); captures['grants'].append(grant); return grant
    monkeypatch.setattr(local.oauth,'accept_exchange_result',accept)
    return captures
