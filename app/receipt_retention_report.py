"""Offline size/retention assessment. Synthetic by default; never deletes.

Optional --inventory accepts explicit metadata-only JSON, never scans source
folders or credentials. Legacy items without retention metadata are rejected.
"""
from datetime import datetime, timezone
import argparse
import json
from pathlib import Path
from uuid import UUID
from .receipt_audit import event, digest, current_after, receipt, encoded, EVENT_TYPES, REASONS
from .receipt_retention import metadata, event_capacity, cleanup_dry_run
from .pdf_page_identity import page_identity


def samples():
    h=digest('synthetic-original');source='synthetic-original-source-123456789'
    base=dict(source_file_id=source,source_content_hash=h,page_count=14,page_number=1,
        page_identity=page_identity(h,1,14),receipt_unit_id='receipt-unit-v1:'+digest('receipt-position'),
        review_identity=digest('review'),revision=2)
    rows=[]
    for n,kind in enumerate(sorted(EVENT_TYPES),1):
        same=kind=='reconciled_existing';different=kind=='confirmed_distinct'
        rows.append(event(base,request_id=str(UUID(int=n)),request_digest=digest(['request',n]),event_type=kind,
            ledger_id='' if different or kind=='hga_confirmed' else 'R-synthetic-existing-ledger-01',
            candidate_ledger_id='R-synthetic-existing-ledger-01' if same or different else '',
            decision='same' if same else 'different' if different else 'skipped' if kind=='intentionally_skipped' else 'confirmed',
            actor_id=digest(['https://accounts.google.com','synthetic-owner']),confirmed_at='2026-10-08T00:00:00+00:00',
            authority_digest=digest(['authority',n]),reason_code=REASONS[kind],
            provenance={'date':'gemini','amount':'human_override','category':'human'}))
    return rows


def fixture_inventory():
    def item(kind,key,size,created):return metadata(kind,created,object_id=key,byte_size=size)
    return [item('analysis_details','synthetic-old-analysis',65536,'2026-01-01T00:00:00+00:00'),
            item('retry_diagnostics','synthetic-old-retry',8192,'2026-01-01T00:00:00+00:00'),
            item('oauth_session','synthetic-expired-session',1024,'2026-10-07T23:00:00+00:00'),
            item('oauth_session','synthetic-fresh-session',1024,'2026-10-08T00:00:00+00:00'),
            item('authority_decision','synthetic-permanent-authority',2048,'2020-01-01T00:00:00+00:00'),
            item('original_source','synthetic-original',15000000,'2020-01-01T00:00:00+00:00'),
            {'object_id':'synthetic-legacy-unclassified','byte_size':4096}]


def report(inventory=None, now='2026-10-08T00:00:00+00:00'):
    rows=samples();capacity=event_capacity(rows)
    marker_mean=sum(len(encoded(receipt(e))) for e in rows)/len(rows)
    current_mean=sum(len(encoded(current_after(e,0))) for e in rows)/len(rows)
    # Worst-case: each event is a new entity/pair. Usually many events share a
    # single current doc, so this conservative bound exceeds actual metadata.
    pair_size=len(encoded({'decision':'different','event_ref':{'year':'2026','event_id':'0'*64},'authority_digest':'0'*64}))
    capacity.update(replay_receipt_mean_bytes=marker_mean,current_mean_bytes=current_mean,
        pair_max_sample_bytes=pair_size,conservative_all_metadata_bytes={str(n):round(n*(capacity['mean_bytes']+marker_mean+current_mean+pair_size))
            for n in (10000,100000,1000000)},backend_overhead_included=False,original_bytes_included=False)
    return {'scope':'offline_synthetic' if inventory is None else 'provided_metadata_only',
        'capacity':capacity,'cleanup':cleanup_dry_run(fixture_inventory() if inventory is None else inventory,now),
        'production_cleanup_activated':False,'live_writes':0,'physical_deletes':0}


def main():
    parser=argparse.ArgumentParser();parser.add_argument('--inventory',type=Path);parser.add_argument('--now')
    args=parser.parse_args()
    inventory=None
    if args.inventory:
        if args.inventory.stat().st_size>1024*1024:raise ValueError('inventory_size_limit')
        inventory=json.loads(args.inventory.read_text('utf-8'))
        if not isinstance(inventory,list) or len(inventory)>10000:raise ValueError('inventory_invalid')
    now = args.now or (datetime.now(timezone.utc).isoformat() if inventory is not None
                       else '2026-10-08T00:00:00+00:00')
    print(json.dumps(report(inventory,now),sort_keys=True))


if __name__=='__main__': main()
