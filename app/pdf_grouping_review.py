"""Manual-only Actions worker for Drive grouping state and management UI.

No receipt pipeline, AI adapter, Medical observer or archive writer is imported.
Source reads always use the existing read-only credential scopes.
"""
from dataclasses import dataclass
from hashlib import sha256
import json
import os
from pathlib import Path
import re

from .drive_run_state import StateError
from .conditional_drive_state_v2 import (
    ConditionalDriveStateTransportV2, conditional_drive_state_service_v2,
)
from .pdf_grouping_authority import DriveGroupingStore, DurablePdfGrouping
from .pdf_grouping_ui import GroupingSheet, process_request
from .receipt_pdf_units import _digest, observe_pdf


@dataclass(frozen=True)
class GroupingBinding:
    folder_id: str
    file_id: str


class DrivePdfReader:
    def __init__(self, service, inbox_folder):
        self.service, self.inbox = service, inbox_folder

    def metadata(self, source_id):
        if not isinstance(source_id, str) or not re.fullmatch(r'[A-Za-z0-9_-]{10,150}', source_id):
            raise StateError('grouping_source_invalid')
        meta = self.service.files().get(fileId=source_id, supportsAllDrives=True,
            fields='id,parents,mimeType,version,trashed').execute(num_retries=0)
        if (meta.get('id') != source_id or meta.get('trashed') or meta.get('parents') != [self.inbox] or
                meta.get('mimeType') != 'application/pdf' or not meta.get('version')):
            raise StateError('grouping_source_changed')
        return meta

    def __call__(self, source_id, previous):
        before = self.metadata(source_id)
        content = self.service.files().get_media(fileId=source_id, supportsAllDrives=True).execute(num_retries=0)
        if self.metadata(source_id) != before:
            raise StateError('grouping_source_changed')
        known = {}
        if previous and previous['source_content_hash'] == sha256(content).hexdigest():
            known = {p['page_number']: p['classification'] for p in previous['pages']
                     if p['classification'] != 'normal'}
        return observe_pdf(content, source_id, known_page_classifications=known)

    def source_ids(self):
        if not re.fullmatch(r'[A-Za-z0-9_-]{10,150}', self.inbox):
            raise StateError('grouping_inbox_invalid')
        value = self.service.files().list(q=f"'{self.inbox}' in parents and trashed=false and mimeType='application/pdf'",
            pageSize=50, fields='nextPageToken,files(id)', supportsAllDrives=True,
            includeItemsFromAllDrives=True).execute(num_retries=0)
        if value.get('nextPageToken'):
            raise StateError('grouping_refresh_limit')
        return [f['id'] for f in value.get('files', [])]

    def verify_pages(self, source_id, previous, *, numbers=None):
        """No OCR/payment extraction: rehash fresh visible pixels at saved scales.

        Used ONLY for confirming an already fully observed proposal. Changed
        bytes or renderer output invalidates the view instead of granting rights.
        """
        from contextlib import closing
        import pypdfium2 as pdfium
        from .receipt_pdf_units import (MAX_SOURCE_BYTES, MAX_PAGE_PIXELS, _render_png,
                                        PageObservation, PdfObservations)
        from .pdf_bounded_rendering import WorkBudget, render_scale
        if not previous:
            raise StateError('page_kind_source_changed')
        before=self.metadata(source_id)
        content=self.service.files().get_media(fileId=source_id,supportsAllDrives=True).execute(num_retries=0)
        if (len(content)>MAX_SOURCE_BYTES or sha256(content).hexdigest()!=previous['source_content_hash'] or
                self.metadata(source_id)!=before):
            raise StateError('page_kind_source_changed')
        budget=WorkBudget();pages=[]
        try:
            if numbers is not None and (not isinstance(numbers,list) or not numbers or
                    any(type(n) is not int or not 1<=n<=previous['page_count'] for n in numbers)):
                raise StateError('page_kind_page_changed')
            for saved in previous['pages']:
                if numbers is not None and saved['page_number'] not in numbers:
                    # Fresh whole-PDF hash still matches. Only the requested
                    # page(s) need pixel verification during repeated manual
                    # owner-input checks; no rights for other pages are added.
                    pages.append(PageObservation(**saved))
                    continue
                scale=saved.get('effective_render_scale')
                if not scale:
                    raise StateError('page_kind_page_unobserved')
                with closing(pdfium.PdfDocument(content)) as document:
                    if len(document)!=previous['page_count']:raise StateError('page_kind_source_changed')
                    with closing(document[saved['page_number']-1]) as page:
                        w,h=page.get_size()
                        maximum,pixels=render_scale(w,h,MAX_PAGE_PIXELS)
                        if scale>maximum:raise StateError('page_kind_page_changed')
                        # Charge the full allowed page cap, even when smaller.
                        with budget.page(pixels):
                            image=_render_png(page,scale,MAX_PAGE_PIXELS)
                            if sha256(image).hexdigest()!=saved['page_hash']:
                                raise StateError('page_kind_page_changed')
                            image=None
                pages.append(PageObservation(**saved))
            if self.metadata(source_id)!=before:raise StateError('page_kind_source_changed')
            return PdfObservations(source_id,previous['source_content_hash'],tuple(pages))
        except StateError:raise
        except Exception:raise StateError('page_kind_page_verification_failed') from None


def preflight_permissions(service, binding, owner_digest, sa_email):
    # Exactly the existing owner + SA writer ACL; never add/remove permissions.
    for target in (binding.folder_id, binding.file_id):
        meta = service.files().get(fileId=target, supportsAllDrives=True,
            fields='owners(emailAddress),permissions(type,role,emailAddress,deleted)').execute(num_retries=0)
        owners = [o.get('emailAddress') for o in meta.get('owners', [])]
        if len(owners) != 1 or not isinstance(owners[0], str) or _digest(owners[0]) != owner_digest:
            raise StateError('grouping_private_permissions_mismatch')
        owner_email = owners[0]
        grants = {(p.get('type'), p.get('role'), p.get('emailAddress')) for p in meta.get('permissions', [])
                  if not p.get('deleted')}
        if grants != {('user', 'owner', owner_email), ('user', 'writer', sa_email)}:
            raise StateError('grouping_private_permissions_mismatch')


def require_manual_context(env):
    if (env.get('GITHUB_ACTIONS') != 'true' or env.get('GITHUB_EVENT_NAME') != 'workflow_dispatch' or
            env.get('GITHUB_REPOSITORY') != 'tmoriuchi1401-source/kakeibo-ai' or
            env.get('GITHUB_REF') != 'refs/heads/main' or
            not env.get('KAKEIBO_VALIDATED_MAIN_SHA') or
            env.get('GITHUB_SHA') != env.get('KAKEIBO_VALIDATED_MAIN_SHA') or
            env.get('PDF_GROUPING_REVIEW_ENABLED') != 'true' or
            env.get('GITHUB_WORKFLOW_REF') != 'tmoriuchi1401-source/kakeibo-ai/.github/workflows/pdf-grouping-review.yml@refs/heads/main' or
            env.get('GEMINI_API_KEY') or env.get('ACTIONS_STEP_DEBUG') == 'true' or env.get('RUNNER_DEBUG') == '1'):
        raise StateError('grouping_manual_context_required')


def open_context(env):
    require_manual_context(env)
    from .google_clients import drive_service, read_only_drive_service, sheets_service
    from .drive_receipts import normalize_folder_id
    from .private_state_bindings import unwrap
    from .settings import Settings, service_account_source
    settings = Settings()
    settings.validate(need_sheet=True, need_drive=True)
    path, info = service_account_source()
    info = info or json.loads(Path(path).read_bytes())
    config = json.loads(env['PDF_GROUPING_BINDING'])
    folder = unwrap('KAKEIBO_STATE_FOLDER_ID', config['folder'], info['private_key'])
    file_id = unwrap('PDF_GROUPING_STATE_FILE_ID', config['file'], info['private_key'])
    binding = GroupingBinding(folder, file_id)
    drive = drive_service()
    preflight_permissions(drive, binding, config['owner_digest'], info['client_email'])
    store = DriveGroupingStore(ConditionalDriveStateTransportV2(conditional_drive_state_service_v2(), binding),
                              _digest([folder, file_id, settings.spreadsheet_id]),
        preflight=lambda: preflight_permissions(drive, binding, config['owner_digest'], info['client_email']))
    store.load()  # Missing/invalid state or unavailable conditional writes stop before UI writes.
    reader = DrivePdfReader(read_only_drive_service(), normalize_folder_id(settings.receipt_drive_folder_id))
    return DurablePdfGrouping(store, reader), GroupingSheet(sheets_service(), settings.spreadsheet_id), reader


def execute(env):
    grouping, sheet, reader = open_context(env)
    mode = env.get('PDF_GROUPING_MODE')
    from .pdf_page_review import SCHEMA
    if mode == 'install-pages' or sheet._get('PDFページ確認','C1') == [[SCHEMA]]:
        from .pdf_page_review_worker import execute_shared
        return execute_shared(env,grouping,sheet,reader,'install' if mode=='install-pages' else mode)
    if mode == 'install':
        sheet.install()
        return {'ui_installed': 1}
    if mode == 'refresh':
        count = held = 0
        for source_id in reader.source_ids():
            view = grouping.display(source_id)
            if view['proposal'] is None:
                held += 1
            else:
                sheet.publish(view)
                count += 1
        return {'displayed': count, 'held': held}
    if mode == 'review':
        return process_request(grouping, sheet, env.get('PDF_GROUPING_REQUEST_ID', ''))
    raise StateError('grouping_operation_invalid')


def main():
    try:
        result = execute(dict(os.environ))
        print(json.dumps(result))  # Only counts/result codes, no identities or source data.
        return 0
    except StateError as error:
        print(json.dumps({'status': 'grouping_required', 'reason': str(error)}))
        return 1
    except Exception:
        print(json.dumps({'status': 'grouping_required', 'reason': 'grouping_operation_failed'}))
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
