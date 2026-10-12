"""Runner-local Docker only: no auth, registry push, Cloud CLI or deployment."""
from datetime import datetime, timezone
import json
from pathlib import Path
import re
import subprocess
import sys
from .prepare_context import ROOT, prepare


def run(*args, **kwargs):
    return subprocess.run(args, check=True, text=True, **kwargs)


def output(*args): return subprocess.check_output(args, text=True).strip()


def main():
    run('docker', 'version')
    assert output('docker','info','--format','{{.OSType}}')=='linux'
    revision=output('git','rev-parse','HEAD')
    assert re.fullmatch('[0-9a-f]{40}',revision)
    context=ROOT.parent/'private-audit'/('staging-build-'+revision)
    prepare(context)
    run(sys.executable,'-m','pip','download','--only-binary=:all:','--require-hashes',
        '-r',str(ROOT/'staging_shadow/requirements.lock'),'-d',str(context/'wheels'))
    (context/'.dockerignore').write_text('*\n!Dockerfile\n!requirements.lock\n!wheels/**\n!bundle/**\n',encoding='utf-8')
    created=datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')
    image='kakeibo-staging-local:'+revision[:12]
    args=('docker','build','--network=none','--platform=linux/amd64','--build-arg','SOURCE_REVISION='+revision,
        '--build-arg','CREATED='+created,'-t',image,str(context))
    run(*args)
    first=output('docker','image','inspect',image,'--format','{{.Id}}')
    run(*args)
    assert output('docker','image','inspect',image,'--format','{{.Id}}')==first
    labels=json.loads(output('docker','image','inspect',image,'--format','{{json .Config.Labels}}'))
    assert labels['org.opencontainers.image.revision']==revision
    assert labels['org.opencontainers.image.source']=='https://github.com/tmoriuchi1401-source/kakeibo-ai'
    assert labels['org.opencontainers.image.created']==created
    isolation=('docker','run','--network=none','--read-only','--cap-drop=ALL',
        '--security-opt=no-new-privileges','--pids-limit=128','--memory=256m','--tmpfs','/tmp:rw,noexec,nosuid,size=32m')
    cid=output(*isolation,'-d',image)
    assert re.fullmatch('[0-9a-f]{64}',cid)
    try:
        # No published port or bind mount; only Docker exec reaches loopback.
        inspect=json.loads(output('docker','inspect',cid))[0]
        assert inspect['HostConfig']['NetworkMode']=='none'
        assert not inspect['HostConfig']['PortBindings'] and not inspect['HostConfig']['Binds']
        assert inspect['Config']['User']=='10001:10001'
        code="import time,urllib.request\nfor i in range(30):\n try:\n  r=urllib.request.urlopen(urllib.request.Request('http://127.0.0.1:8080/health',headers={'X-Forwarded-Proto':'https'}),timeout=2);assert r.status==200;break\n except OSError:time.sleep(.2)\nelse:raise AssertionError('staging did not start')\nprint('STAGING ENTRY HTTP HEALTH PASSED')"
        run('docker','exec',cid,'python','-c',code)
        run('docker','exec',cid,'python','-m','staging_shadow.smoke')
        # Misconfigured credentials/authorities must fail before file/client IO.
        for name in ('HGA_REAL_PAGE_CONFIG_PATH','HGA_DRIVE_CREDENTIAL_PATH','HGA_CONFIG_PATH',
                     'GOOGLE_APPLICATION_CREDENTIALS','GOOGLE_CLOUD_PROJECT','SPREADSHEET_ID',
                     'GEMINI_API_KEY','UNIFIED_RECEIPT_WRITE_ENABLED','HTTPS_PROXY'):
            result=subprocess.run((*isolation,'--rm','-e',name+'=synthetic-forbidden',image),capture_output=True,text=True,timeout=20)
            assert result.returncode!=0,'production configuration accepted'
        print(json.dumps(dict(source_revision=revision,local_image_id=first,OCI_labels_readback=True,
            build_repeat_same_inputs=True,production_configuration_rejections=9,registry_push=0,
            cloud_auth=0,deploy=0,published_ports=0)))
    finally:
        run('docker','stop','--time','10',cid,stdout=subprocess.DEVNULL)
        assert output('docker','inspect',cid,'--format','{{.State.Running}}')=='false'
        run('docker','rm',cid,stdout=subprocess.DEVNULL)
        print('STAGING CONTAINER STOPPED AND REMOVED')


if __name__=='__main__':main()
