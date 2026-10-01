"""Local PDF grouping proposals and human-confirmed, payload-only authority.

No AI, ledger, Drive, or Medical clients belong in this module. Confirmation
authorizes construction of a rendered image, never production accounting.
Run ``python -m app.receipt_pdf_grouping --help`` for the local review CLI.
"""
from __future__ import annotations

import argparse
from contextlib import contextmanager
from dataclasses import dataclass
from hashlib import sha256
from io import BytesIO
import json
import os
from pathlib import Path
import re
import tempfile
from typing import Protocol

from .receipt_pdf_units import (
    MAX_DOCUMENT_PIXELS, MAX_PAYLOAD_BYTES, MAX_PDF_PAGES, PdfObservations,
    PdfUnitManifestStore, _checked_gate, _digest, document_result, observe_pdf,
    valid_render_metadata,
)
from .receipt_privacy_gate import require_receipt_ai_permission
from .receipt_text_extraction import _extract_receipt_text

PROPOSAL_VERSION = 'adjacent-local-v1'
CLASSIFICATIONS = frozenset({'normal', 'medical', 'payroll', 'sensitive_unknown'})


class GroupingError(ValueError):
    """Data-free diagnostic safe to display without leaking OCR or source data."""


def privacy_for(classifications):
    kinds = set(classifications)
    if not kinds or not kinds <= CLASSIFICATIONS or 'sensitive_unknown' in kinds:
        return 'sensitive_unknown'
    if {'medical', 'payroll'} <= kinds:
        return 'sensitive_unknown'
    if 'medical' in kinds:
        return 'medical'
    if 'payroll' in kinds:
        return 'payroll'
    return 'normal'


def _unit_status(kind):
    return ('normal_preview_ready' if kind == 'normal' else
            'medical_pending' if kind == 'medical' else 'privacy_pending')


def _snapshot(observations):
    pages = observations.pages
    if (observations.status != 'observed' or not 2 <= len(pages) <= MAX_PDF_PAGES or
            [p.page_number for p in pages] != list(range(1, len(pages) + 1)) or
            any(p.source_file_id != observations.source_file_id or
                p.source_content_hash != observations.source_content_hash or
                p.classification not in CLASSIFICATIONS or
                not valid_render_metadata(p.metadata()) or
                not re.fullmatch(r'[0-9a-f]{64}', p.page_hash) for p in pages)):
        raise GroupingError('grouping_observation_unavailable')
    return {'source_file_id': observations.source_file_id,
            'source_content_hash': observations.source_content_hash,
            'page_count': len(pages), 'pages': [p.metadata() for p in pages]}


@dataclass(frozen=True)
class PageEvidence:
    # Evidence is ephemeral; raw names, dates, receipt numbers and OCR are not
    # written to the proposal. Digests are hints, never authority or unit IDs.
    issuer: str = ''
    date: str = ''
    receipt: str = ''
    printed_page: int = 0
    printed_count: int = 0
    has_total: bool = False
    continuation: bool = False


def evidence_from_text(text: str) -> PageEvidence:
    def hint(pattern):
        match = re.search(pattern, text, re.IGNORECASE | re.MULTILINE)
        return _digest(match.group(1).strip().casefold()) if match else ''
    numbering = re.search(r'(?<!\d)(\d{1,2})\s*/\s*(\d{1,2})(?!\d)', text)
    return PageEvidence(
        issuer=hint(r'^(?:店舗名|施設名|店名|merchant|store)\s*[:：]\s*(.+)$'),
        date=hint(r'(\d{4}[-/.年]\d{1,2}[-/.月]\d{1,2}日?)'),
        receipt=hint(r'(?:レシート番号|伝票番号|receipt\s*(?:no\.?|number))\s*[:：#]?\s*([\w-]+)'),
        printed_page=int(numbering[1]) if numbering else 0,
        printed_count=int(numbering[2]) if numbering else 0,
        has_total=bool(re.search(r'合計|総額|\btotal\b', text, re.IGNORECASE)),
        continuation=bool(re.search(r'続き|次頁|次ページ|繰越|continued|carry\s*forward', text, re.IGNORECASE)),
    )


def local_evidence(observations):
    result = []
    for page in observations.pages:
        if page.effective_render_scale is not None:
            result.append(PageEvidence(**page.grouping_hints) if page.grouping_hints
                          and page.classification == 'normal' and page.observation_complete else PageEvidence())
            continue
        if page.classification != 'normal' or page._payload is None:
            result.append(PageEvidence())
            continue
        extraction = _extract_receipt_text(page._payload, 'image/png')
        if extraction.status != 'extracted' or not extraction.observation_complete:
            raise GroupingError('grouping_evidence_incomplete')
        result.append(evidence_from_text(extraction.text))
    return tuple(result)


class GroupingProposer(Protocol):
    """UI-independent candidate interface; candidates carry no authority."""
    def propose(self, observations: PdfObservations): ...


class AdjacentPageGrouping:
    def __init__(self, evidence_provider=local_evidence):
        self.evidence_provider = evidence_provider

    def propose(self, observations):
        evidence = self.evidence_provider(observations)
        if len(evidence) != len(observations.pages):
            raise GroupingError('grouping_evidence_incomplete')
        groups = []
        for i, page in enumerate(observations.pages):
            reason, confidence = 'insufficient_continuation_evidence', 0.0
            merge = False
            if i and page.classification == observations.pages[i - 1].classification == 'normal':
                left, right = evidence[i - 1], evidence[i]
                anchors = (left.issuer and left.issuer == right.issuer and
                           left.date and left.date == right.date and
                           left.receipt and left.receipt == right.receipt)
                numbered = (left.printed_count > 1 and
                            left.printed_count == right.printed_count and
                            1 <= left.printed_page < right.printed_page <= right.printed_count and
                            right.printed_page == left.printed_page + 1)
                # Similar layout, store/date alone, or adjacency alone cannot
                # establish a transaction. An earlier total closes the chain.
                if anchors and not left.has_total and (numbered or left.continuation):
                    merge = True
                    reason = 'matching_transaction_and_page_sequence' if numbered else 'matching_transaction_and_continuation'
                    confidence = 0.95 if numbered else 0.8
            if merge:
                groups[-1]['page_numbers'].append(page.page_number)
                groups[-1].update(reason=reason, confidence=confidence)
            else:
                groups.append({'page_numbers': [page.page_number],
                               'reason': reason, 'confidence': confidence})
        return groups


def _partition(groups, page_count):
    try:
        ranges = [g['page_numbers'] for g in groups]
        if (not ranges or any(not numbers or
                any(type(n) is not int for n in numbers) or
                numbers != list(range(numbers[0], numbers[-1] + 1)) for numbers in ranges) or
                [n for numbers in ranges for n in numbers] != list(range(1, page_count + 1))):
            raise ValueError()
    except (KeyError, TypeError, ValueError, IndexError):
        raise GroupingError('grouping_partition_invalid') from None


def _proposal(snapshot, candidates, revision, status='proposed'):
    _partition(candidates, snapshot['page_count'])
    groups = []
    for candidate in candidates:
        if (type(candidate['confidence']) not in (int, float) or
                not 0 <= candidate['confidence'] <= 1 or candidate['reason'] not in {
                    'insufficient_continuation_evidence', 'matching_transaction_and_page_sequence',
                    'matching_transaction_and_continuation', 'human_partition'}):
            raise GroupingError('grouping_evidence_invalid')
        numbers = candidate['page_numbers']
        members = [snapshot['pages'][n - 1] for n in numbers]
        kinds = [p['classification'] for p in members]
        groups.append({'group_id': 'group-' + _digest([snapshot['source_content_hash'], revision, numbers]),
                       'page_numbers': numbers, 'page_range': [numbers[0], numbers[-1]],
                       'member_page_hashes': [p['page_hash'] for p in members],
                       'page_classifications': kinds, 'proposed_group_type': privacy_for(kinds),
                       'confidence': candidate['confidence'], 'reason': candidate['reason'],
                       'status': status})
    value = {**snapshot, 'proposal_version': PROPOSAL_VERSION,
             'grouping_version': revision, 'status': status, 'groups': groups}
    return {**value, 'proposal_digest': _digest(value)}


def _valid_proposal(proposal, snapshot):
    try:
        if (any(proposal[k] != v for k, v in snapshot.items()) or
                proposal['proposal_version'] != PROPOSAL_VERSION or
                type(proposal['grouping_version']) is not int or proposal['grouping_version'] < 1 or
                proposal['status'] not in {'proposed', 'rejected'}):
            return False
        candidates = [{'page_numbers': g['page_numbers'], 'confidence': g['confidence'],
                       'reason': g['reason']} for g in proposal['groups']]
        return proposal == _proposal(snapshot, candidates, proposal['grouping_version'], proposal['status'])
    except (KeyError, TypeError, ValueError):
        return False


def _confirmation(proposal):
    # Deliberately a separate record: editing status on a proposal grants nothing.
    return {'status': 'confirmed', 'source_file_id': proposal['source_file_id'],
            'source_content_hash': proposal['source_content_hash'],
            'page_count': proposal['page_count'], 'proposal_digest': proposal['proposal_digest'],
            'grouping_version': proposal['grouping_version'],
            'groups': [{**g, 'status': 'confirmed',
                        'unit_status': _unit_status(privacy_for(g['page_classifications']))}
                       for g in proposal['groups']],
            'authority_scope': 'rendered_payload_only', 'accounting_allowed': False}


@dataclass(frozen=True)
class ConfirmedDocumentUnit:
    source_file_id: str
    source_content_hash: str
    page_numbers: tuple[int, ...]
    member_page_hashes: tuple[str, ...]
    page_classifications: tuple[str, ...]
    grouping_version: int
    proposal_digest: str

    @property
    def unit_id(self):
        return 'pdf-confirmed-unit-v1:' + _digest([
            self.source_file_id, self.source_content_hash, self.page_numbers,
            self.member_page_hashes, self.grouping_version, self.proposal_digest])

    @property
    def classification(self):
        return privacy_for(self.page_classifications)

    def metadata(self):
        kind = self.classification
        return {**vars(self), 'unit_id': self.unit_id, 'classification': kind,
                'status': _unit_status(kind),
                'authority_scope': 'rendered_payload_only', 'accounting_allowed': False,
                'archive_allowed': False}


class GroupingStore:
    """Atomic private local records; one current snapshot per source identity.

    This is a trusted local operator store, not a signature or remote authority.
    Future web/Spreadsheet confirmation must authenticate the human separately.
    """
    def __init__(self, directory='.private/pdf-grouping'):
        self.directory = Path(directory)

    @contextmanager
    def record(self, source_file_id):
        if os.getenv('GITHUB_ACTIONS') == 'true':
            raise GroupingError('local_grouping_is_not_actions_authority')
        self.directory.mkdir(parents=True, exist_ok=True)
        path = self.directory / (_digest(source_file_id) + '.json')
        lock = path.with_suffix('.lock')
        try:
            fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        except FileExistsError:
            raise GroupingError('grouping_store_busy') from None
        os.close(fd)
        try:
            try:
                value = json.loads(path.read_text(encoding='utf-8')) if path.exists() else {}
                if not isinstance(value, dict):
                    raise ValueError()
            except (ValueError, OSError):
                raise GroupingError('grouping_store_invalid') from None
            yield value
            temporary = None
            try:
                with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', dir=self.directory,
                                                 delete=False) as output:
                    temporary = Path(output.name)
                    json.dump(value, output, ensure_ascii=True)
                    output.flush()
                    os.fsync(output.fileno())
                os.replace(temporary, path)
            finally:
                if temporary is not None and temporary.exists():
                    temporary.unlink()
        finally:
            lock.unlink()


class PdfGroupingService:
    """Local service shared by the CLI and a future authenticated review UI."""
    def __init__(self, store=None, proposer=None, observation_store=None):
        self.store = store or GroupingStore()
        self.proposer = proposer or AdjacentPageGrouping()
        # Share existing restrictive provenance with ordinary PDF ingestion.
        self.observation_store = observation_store or PdfUnitManifestStore()

    def observe(self, content, source_file_id, *, known_source_classification=None):
        known = self.observation_store.restrictions(source_file_id, sha256(content).hexdigest())
        observed = observe_pdf(content, source_file_id, known_page_classifications=known,
                               known_source_classification=known_source_classification)
        # The grouping CLI cannot overwrite a single-page accounting manifest.
        if len(observed.pages) == 1:
            return observed
        self.observation_store.save(document_result(observed))
        return observed

    def prepare(self, observations):
        try:
            snapshot = _snapshot(observations)
        except GroupingError:
            with self.store.record(observations.source_file_id) as record:
                revision = self._revision(record) + 1
                record.clear()
                record.update(status='grouping_required', reason='grouping_observation_unavailable', revision=revision)
                return self._view(record)
        with self.store.record(observations.source_file_id) as record:
            if not _valid_proposal(record.get('proposal', {}), snapshot):
                revision = self._revision(record) + 1
                record.clear()
                record['revision'] = revision
                try:
                    record['proposal'] = _proposal(snapshot, self.proposer.propose(observations), revision)
                except Exception:
                    record.update(status='grouping_required', snapshot=snapshot,
                                  reason='grouping_proposal_failed')
            proposal = record.get('proposal')
            if proposal and (proposal['status'] != 'proposed' or
                             record.get('confirmation') != _confirmation(proposal)):
                record.pop('confirmation', None)
            return self._view(record)

    @staticmethod
    def _revision(record):
        proposal = record.get('proposal')
        values = [record.get('revision', 0),
                  proposal.get('grouping_version', 0) if isinstance(proposal, dict) else 0]
        return max((v for v in values if type(v) is int and v >= 0), default=0)

    @staticmethod
    def _view(record):
        return {'status': 'grouping_confirmed' if record.get('confirmation') else 'grouping_required',
                'proposal': record.get('proposal'), 'confirmation': record.get('confirmation'),
                'reason': record.get('reason'), 'accounting_allowed': False, 'archive_allowed': False}

    def review(self, observations, expected_digest, *, action, page_groups=None):
        """Explicit human mutation; no automatic caller invokes this method.

        Compare the exact reviewed proposal, then either confirm, reject or edit.
        Edits make a new unconfirmed proposal even when the partition is unchanged.
        """
        # Persist source-change invalidation before rejecting a stale review.
        self.prepare(observations)
        snapshot = _snapshot(observations)
        with self.store.record(observations.source_file_id) as record:
            proposal = record.get('proposal', {})
            if not _valid_proposal(proposal, snapshot) or proposal['proposal_digest'] != expected_digest:
                raise GroupingError('grouping_review_stale')
            if action == 'confirm':
                if page_groups is not None or proposal['status'] != 'proposed':
                    raise GroupingError('grouping_action_invalid')
                record['confirmation'] = _confirmation(proposal)
            elif action == 'edit':
                candidates = [{'page_numbers': numbers, 'confidence': 1.0, 'reason': 'human_partition'}
                              for numbers in page_groups or []]
                edited = _proposal(snapshot, candidates, proposal['grouping_version'] + 1)
                if page_groups != [g['page_numbers'] for g in proposal['groups']]:
                    record['proposal'] = edited
                    record['revision'] = record['proposal']['grouping_version']
                    record.pop('confirmation', None)
            elif action == 'reject':
                record.pop('confirmation', None)
                candidates = [{'page_numbers': g['page_numbers'], 'confidence': g['confidence'],
                               'reason': g['reason']} for g in proposal['groups']]
                record['proposal'] = _proposal(snapshot, candidates, proposal['grouping_version'], 'rejected')
            else:
                raise GroupingError('grouping_action_invalid')
            return self._view(record)

    def units(self, observations):
        view = self.prepare(observations)
        confirmed = view['confirmation']
        if confirmed is None:
            return ()
        return tuple(ConfirmedDocumentUnit(
            confirmed['source_file_id'], confirmed['source_content_hash'],
            tuple(g['page_numbers']), tuple(g['member_page_hashes']), tuple(g['page_classifications']),
            confirmed['grouping_version'], confirmed['proposal_digest']) for g in confirmed['groups'])

    def payload(self, observations, unit_id):
        """Fresh PNG for offline/mock analysis; revalidate authority and pixels.

        Returning bytes grants no ledger authority. The production multi-page
        ReceiptPipeline continues to stop at grouping_required independently.
        """
        from PIL import Image
        unit = next((unit for unit in self.units(observations) if unit.unit_id == unit_id), None)
        if unit is None or unit.classification != 'normal':
            raise GroupingError('grouping_payload_forbidden')
        images = []
        try:
            for n, expected in zip(unit.page_numbers, unit.member_page_hashes):
                page = observations.pages[n - 1]
                payload = observations.page_payload(n)
                if payload is None or sha256(payload).hexdigest() != expected:
                    raise GroupingError('grouping_payload_identity_changed')
                gate, complete = _checked_gate(payload, page.classification)
                if (not complete or gate.classification != 'normal' or
                        gate.gemini_allowed is not True or gate.status != 'ready_for_gemini'):
                    raise GroupingError('grouping_payload_privacy_blocked')
                require_receipt_ai_permission(payload, 'image/png', known_source_classification='normal')
                with Image.open(BytesIO(payload)) as image:
                    images.append(image.convert('RGB'))
            width, height = max(im.width for im in images), sum(im.height for im in images)
            if width * height > MAX_DOCUMENT_PIXELS:
                raise GroupingError('grouping_payload_limit')
            with Image.new('RGB', (width, height), 'white') as combined:
                offset = 0
                for image in images:
                    combined.paste(image, (0, offset))
                    offset += image.height
                output = BytesIO()
                combined.save(output, format='PNG')
            result = output.getvalue()
            if len(result) > MAX_PAYLOAD_BYTES:
                raise GroupingError('grouping_payload_limit')
            gate, complete = _checked_gate(result, 'normal')
            if (not complete or gate.classification != 'normal' or
                    gate.gemini_allowed is not True or gate.status != 'ready_for_gemini'):
                raise GroupingError('grouping_payload_privacy_blocked')
            require_receipt_ai_permission(result, 'image/png', known_source_classification='normal')
            return result
        finally:
            for image in images:
                image.close()


def main(argv=None):
    parser = argparse.ArgumentParser(description='Local PDF grouping review; no external clients')
    parser.add_argument('action', choices=('show', 'edit', 'confirm', 'reject', 'units', 'payload'))
    parser.add_argument('pdf')
    parser.add_argument('--source-file-id', required=True)
    parser.add_argument('--state-dir', default='.private/pdf-grouping')
    parser.add_argument('--observation-dir', default='.private/pdf-document-units',
                        help='Existing page privacy manifests; share with PDF intake')
    parser.add_argument('--source-classification', choices=('medical', 'payroll', 'sensitive_unknown'))
    parser.add_argument('--expected-proposal')
    parser.add_argument('--groups', help='JSON partition, e.g. [[1,2],[3]]')
    parser.add_argument('--unit-id')
    parser.add_argument('--output', help='Local PNG path for offline/mock analysis only')
    args = parser.parse_args(argv)
    try:
        service = PdfGroupingService(GroupingStore(args.state_dir),
                                     observation_store=PdfUnitManifestStore(args.observation_dir))
        observations = service.observe(Path(args.pdf).read_bytes(), args.source_file_id,
                                       known_source_classification=args.source_classification)
        if args.action in {'edit', 'confirm', 'reject'}:
            if not args.expected_proposal:
                raise GroupingError('grouping_expected_proposal_required')
            if args.action == 'edit' and not args.groups:
                raise GroupingError('grouping_partition_required')
            groups = json.loads(args.groups) if args.groups else None
            result = service.review(observations, args.expected_proposal, action=args.action, page_groups=groups)
        elif args.action == 'units':
            result = {'units': [unit.metadata() for unit in service.units(observations)],
                      'accounting_allowed': False, 'archive_allowed': False}
        elif args.action == 'payload':
            if not args.unit_id or not args.output:
                raise GroupingError('grouping_payload_arguments_required')
            payload = service.payload(observations, args.unit_id)
            # Never overwrite an existing file (including the source PDF).
            with Path(args.output).open('xb') as output:
                output.write(payload)
            result = {'status': 'offline_payload_created', 'accounting_allowed': False}
        else:
            result = service.prepare(observations)
        print(json.dumps(result, ensure_ascii=True))
        return 0
    except GroupingError as exc:
        print(json.dumps({'status': 'grouping_required', 'reason': str(exc)}))
        return 1
    except Exception:
        print(json.dumps({'status': 'grouping_required', 'reason': 'grouping_operation_failed'}))
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
