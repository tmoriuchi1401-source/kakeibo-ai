"""Archive read-back-complete confirmations without changing receipt identity."""
from copy import deepcopy
from hashlib import sha256

from .drive_run_state import StateError


def archive_confirmations(review, inbox, processed, drive, download):
    if not processed:return 0
    if inbox==processed:raise StateError('confirmation_archive_folders_invalid')
    moved=0
    for key,old in list(review.items.items()):
        if (old['status']!='applied' or old['kind'] not in {'normal','medical'}
                or old['folder_id']!=inbox or old.get('archive',{}).get('status')=='complete'):
            continue
        # Historical operator imports have a separate verified archive history.
        if old.get('operator_import'):continue
        if not old.get('plan') or not review._complete(old['plan']):
            raise StateError('confirmation_readback_mismatch')
        source=old['source'];sid=source['source_id']
        def metadata():
            return drive.files().get(fileId=sid,supportsAllDrives=True,
                fields='id,parents,version,mimeType,trashed,appProperties').execute(num_retries=0)
        meta=metadata()
        if meta.get('trashed') or meta.get('mimeType')!=source['mime_type']:
            raise StateError('confirmation_source_changed')
        at_destination=meta.get('parents')==[processed]
        if not at_destination and (meta.get('parents')!=[inbox] or meta.get('version')!=source['version']):
            raise StateError('confirmation_source_changed')
        # Drive metadata versions increase on moves. The saved source identity
        # remains unchanged; content is checked both before and after a move.
        if sha256(download(sid)).hexdigest()!=source['sha256'] or metadata()!=meta:
            raise StateError('confirmation_source_changed')
        item=deepcopy(old)
        if not at_destination:
            item['archive']={'status':'pending','destination':processed}
            review.save_item(key,item)
            if metadata()!=meta:raise StateError('confirmation_source_changed')
            properties=dict(meta.get('appProperties',{}))
            if old['kind']=='medical':properties['kakeiboReceiptClass']='medical'
            try:
                drive.files().update(fileId=sid,addParents=processed,removeParents=inbox,
                    body={'appProperties':properties},fields='id,parents',supportsAllDrives=True).execute(num_retries=0)
            except Exception:
                # Never resend an ambiguous move. Inspect this same file below.
                pass
            after=metadata()
            if (after.get('trashed') or after.get('parents')!=[processed]
                    or after.get('mimeType')!=source['mime_type']
                    or sha256(download(sid)).hexdigest()!=source['sha256'] or metadata()!=after):
                raise StateError('confirmation_archive_readback_required')
            moved+=1
        item['archive']={'status':'complete','destination':processed}
        review.save_item(key,item)
    return moved
