"""Compare saved vision answers only after all ten independent attempts finish."""
import argparse
import hashlib
import json
from pathlib import Path


def comparison(candidate, human, ai):
    return {'existing_candidate_present':candidate is not None,
            'ai_matches_human':None if ai is None or human is None else ai == human,
            'existing_matches_human':None if candidate is None or human is None else candidate == human,
            'ai_matches_existing':None if ai is None or candidate is None else ai == candidate}


def main(root, session):
    results=[]; answers=[]
    for unit in range(1,11):
        folder=root/'ai-results'/f'unit-{unit:02d}'
        result=json.loads((folder/'result.json').read_text(encoding='utf-8'))
        results.append(result)
        answers.append(json.loads((folder/'answer.json').read_text(encoding='utf-8')) if result['status'] in {'readable','ambiguous','unreadable'} else {'amount_yen':None})
    # Only now load the human records. No output of raw amounts or quotations.
    source=session.read_bytes(); digest=hashlib.sha256(source).hexdigest()
    saved=json.loads(source)
    manifest=json.loads((root/'manifest.json').read_text(encoding='utf-8'))
    cards={c['unit']:c for c in saved['units']}
    rows=[]
    for unit,result,answer in zip(range(1,11),results,answers):
        records=[r for r in saved['records'] if r['unit']==unit and r['mode']=='assisted']
        if len(records)!=1: raise ValueError('human_record_not_unique')
        human=records[0]; card=cards[unit]
        if any(human[k]!=card[k] for k in ('page','source_sha256','image_sha256')):
            raise ValueError('human_source_binding_mismatch')
        events=[json.loads(line) for line in (root/'ai-results'/f'unit-{unit:02d}'/'events.jsonl').read_text(encoding='utf-8').splitlines() if line.strip()]
        completed=sum(e.get('type')=='turn.completed' for e in events)
        tools=[e.get('item',{}).get('type') for e in events if e.get('type')=='item.completed' and e.get('item',{}).get('type') not in {'agent_message','reasoning'}]
        rows.append({'unit':unit,'status':result['status'],'elapsed_seconds':result['elapsed_seconds'],
            'human_result_present':human['confirmed_amount'] is not None,
            **comparison(card['original_candidate'],human['confirmed_amount'],answer['amount_yen']),
            'completed_turns':completed,'tool_events':tools,'usage':result['usage']})
    report={'model':'gpt-5.6-sol','input_mode':'fresh_codex_exec_image', 'rows':rows,
        'matches':sum(r['ai_matches_human'] is True for r in rows),
        'new_readable_without_existing_candidate':sum(not r['existing_candidate_present'] and r['status']=='readable' for r in rows),
        'new_matches_without_existing_candidate':sum(not r['existing_candidate_present'] and r['ai_matches_human'] is True for r in rows),
        'preparation_seconds':None,
        'inference_seconds':sum(r['elapsed_seconds'] for r in rows),
        'manual_crop_count':sum(r['source_crop_provenance'] in
            {'manually-narrowed','existing-manual-reused'} for r in manifest['records']),
        'human_session_sha256':digest,'cost_currency':None,'v3_completed_turns':sum(r['completed_turns'] for r in rows),
        'approved_image_submissions':10,'original_image_submissions':0,
        'drive_sheets_writes':0,'production_writes':0}
    assert hashlib.sha256(session.read_bytes()).hexdigest()==digest
    (root/'ai-results'/'comparison.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps(report,ensure_ascii=False))


if __name__=='__main__':
    parser=argparse.ArgumentParser(); parser.add_argument('root',type=Path); parser.add_argument('session',type=Path)
    args=parser.parse_args(); main(args.root,args.session)
