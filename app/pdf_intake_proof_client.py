"""Existing service account -> existing Cloud Run; no owner session access.

The machine ID token is obtained from Google's token endpoint using the
already configured service account. Only proof digests/target references are
sent. Neither session cookies nor owner OIDC tokens are accepted or exported.
"""
from urllib.parse import urlsplit

from .drive_run_state import StateError
from .pdf_intake_authority import digest, page_key


class ProofClient:
    def __init__(self, origin, info, *, session=None):
        from google.oauth2.service_account import IDTokenCredentials
        from google.auth.transport.requests import AuthorizedSession
        parsed = urlsplit(origin)
        if (origin != 'https://'+parsed.netloc or parsed.username or parsed.password
                or parsed.port not in (None,443) or not parsed.hostname
                or not parsed.hostname.endswith('.run.app')):
            raise StateError('pdf_intake_proof_origin_invalid')
        self.origin = origin
        self.http = session or AuthorizedSession(IDTokenCredentials.from_service_account_info(info,target_audience=origin))

    def verify(self, proof, expected):
        query = {'page_key':page_key(expected['page']), 'operation':expected['operation'],
                 'unit_id':expected.get('unit',{}).get('receipt_unit_id'),
                 'binding_digest':digest(expected),'authority_digest':digest(proof)}
        try:
            response=self.http.post(self.origin+'/intake/verify',json=query,timeout=20,allow_redirects=False)
            if response.status_code!=200:raise StateError('pdf_intake_authority_backend_rejected')
            if len(response.content)>2048:raise StateError('pdf_intake_authority_reply_invalid')
            result=response.json()
            if set(result)!={'valid','binding_digest','authority_digest','actor_id','request_id'}:
                raise StateError('pdf_intake_authority_reply_invalid')
            return result
        except StateError:raise
        except Exception:raise StateError('pdf_intake_authority_backend_unavailable') from None

    def archive(self,sid,source_hash,*,attest=False):
        try:
            response=self.http.post(self.origin+'/intake/archive',json={'source_id':sid,'source_hash':source_hash,
                'mode':'attest' if attest else 'lookup'},timeout=90,allow_redirects=False)
            if response.status_code!=200 or len(response.content)>1024:raise StateError('pdf_intake_archive_backend_rejected')
            result=response.json()
            if set(result)!={'archive_allowed','summary_digest','replayed'}:raise StateError('pdf_intake_archive_reply_invalid')
            return result
        except StateError:raise
        except Exception:raise StateError('pdf_intake_archive_outcome_unknown') from None
