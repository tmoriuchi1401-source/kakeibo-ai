"""PDF grouping-only Drive v2 CAS; existing Drive v3 transports stay unchanged.

No provisioning, sharing, movement, unconditional write or write retry exists
here. Callers still validate the private ACL and grouping schema separately.
"""
import io
import re

from .drive_run_state import StateError

MAX_BYTES = 8 * 1024 * 1024


def strong_etag(value):
    # RFC entity-tag syntax, with a nonempty opaque value. Reject W/, *, CR/LF,
    # embedded quotes and controls before constructing any write request.
    return isinstance(value, str) and re.fullmatch(r'"[\x21\x23-\x7e\x80-\xff]+"', value) is not None


def conditional_drive_state_service_v2():
    from googleapiclient.discovery import build
    from .google_clients import credentials
    # Same pre-existing scopes/credential selection; no v3 client is replaced.
    return build('drive', 'v2', credentials=credentials(), cache_discovery=False)


class ConditionalDriveStateTransportV2:
    def __init__(self, service, binding):
        if service._rootDesc.get('version') != 'v2':
            raise StateError('state_conditional_api_mismatch')
        self.service, self.binding = service, binding

    def _metadata(self):
        headers = {}
        request = self.service.files().get(fileId=self.binding.file_id, supportsAllDrives=True,
            fields='id,etag,parents(id),labels(trashed),mimeType,editable')
        request.add_response_callback(lambda response: headers.update(response))
        meta = request.execute(num_retries=0)
        if (meta.get('id') != self.binding.file_id or
                meta.get('parents') != [{'id': self.binding.folder_id}] or
                meta.get('labels', {}).get('trashed') or meta.get('mimeType') != 'application/json'):
            raise StateError('state_drive_target_mismatch')
        tag = meta.get('etag')
        if not strong_etag(tag) or ('etag' in headers and headers['etag'] != tag):
            raise StateError('state_conditional_write_unavailable')
        return meta, tag

    def read_versioned(self):
        try:
            before, tag = self._metadata()
            payload = self.service.files().get_media(fileId=self.binding.file_id,
                supportsAllDrives=True).execute(num_retries=0)
            after, current = self._metadata()
            if before != after or tag != current:
                raise StateError('state_changed_during_read')
            if not isinstance(payload, bytes) or len(payload) > MAX_BYTES:
                raise StateError('state_size_limit')
            return payload, tag
        except StateError:
            raise
        except Exception:
            raise StateError('state_drive_read_failed') from None

    def replace_versioned(self, expected, tag, proposed):
        from googleapiclient.errors import HttpError
        from googleapiclient.http import MediaIoBaseUpload
        if not strong_etag(tag):
            raise StateError('state_conditional_write_unavailable')
        if (not isinstance(expected, bytes) or not isinstance(proposed, bytes) or
                max(len(expected), len(proposed)) > MAX_BYTES):
            raise StateError('state_size_limit')
        current, current_tag = self.read_versioned()
        if current != expected or current_tag != tag:
            raise StateError('state_changed_since_read')
        meta, checked_tag = self._metadata()
        if checked_tag != tag:
            raise StateError('state_changed_since_read')
        if meta.get('editable') is not True:
            raise StateError('state_drive_not_editable')
        request = self.service.files().update(fileId=self.binding.file_id, supportsAllDrives=True,
            media_body=MediaIoBaseUpload(io.BytesIO(proposed), mimetype='application/json', resumable=False),
            fields='id,etag')
        request.headers['If-Match'] = tag
        try:
            request.execute(num_retries=0)
        except Exception as error:
            if isinstance(error, HttpError) and error.resp.status == 412:
                # Never retry a conflict, even if another writer saved equal bytes.
                raise StateError('state_changed_since_read') from None
            # Ambiguous delivery may be acknowledged by exact read-back only.
            # No second update request or unconditional fallback is permitted.
            if self.read_versioned()[0] != proposed:
                raise StateError('state_drive_write_unknown') from None
        if self.read_versioned()[0] != proposed:
            raise StateError('state_save_readback_mismatch')
