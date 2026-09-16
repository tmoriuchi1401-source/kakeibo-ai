"""Coordinate derived-only sending after the AI-key-free intake process exits."""
import base64
from hashlib import sha256
import json
from pathlib import Path
import subprocess
import sys

from .drive_run_state import StateError
from .medical_candidate_state import MedicalCandidateState
from .medical_candidate_preparation import verify_preparation
from .medical_image_candidate import PaymentAnswer, PROMPT_SHA, analysis_key, admit_answer
from .receipt_confirmation import review_id
from .receipt_confirmation_production import open_context

LIMIT=3
SENDER_ENV=('PATH','SYSTEMROOT','WINDIR','TEMP','TMP','PYTHONPATH','PYTHONIOENCODING',
    'GEMINI_API_KEY','GEMINI_MODEL','MEDICAL_CROP_ATTESTATION_KEY','MEDICAL_DERIVED_AI_POLICY')


def send_derived(packet,payload,env):
    child={k:env[k] for k in SENDER_ENV if k in env}
    result=subprocess.run([sys.executable,'-m','app.medical_image_sender'],
        input=json.dumps({'png':base64.b64encode(payload).decode(),'proof':packet['proof']}),
        env=child,capture_output=True,text=True,encoding='utf-8',timeout=120)
    if result.returncode:raise StateError('medical_image_response_unknown')
    try:return PaymentAnswer.model_validate_json(result.stdout)
    except Exception:raise StateError('medical_image_response_unknown') from None


def process_plans(plans,*,state,verify_source,load_crop,send,model,key,identity_key,allow_send=True):
    counts={'medical_ai_requests':0,'medical_ai_reused':0,'medical_ai_candidates':0,'medical_ai_held':0}
    if type(plans) is not list or len(plans)>100:raise StateError('medical_preparation_invalid')
    seen=set()
    for plan in plans:
        source=plan['source'];rid=review_id('medical',source)
        if plan['review_id']!=rid or rid in seen:raise StateError('medical_preparation_identity_invalid')
        seen.add(rid)
        item=state.store.value['confirmation_items'][rid]
        if item['source']!=source:raise StateError('medical_preparation_source_changed')
        verify_source(source,item['folder_id'])
        fields=dict(plan.get('fields',{}))
        if plan['status']=='held':
            fields['review_message']='匿名化確認が必要：安全な支払額領域を確定できないため画像未送信。原本を確認してください。'
            fields['provenance']={'status':'anonymization_held','reason':plan['reason']}
            state.publish(rid,source,fields);counts['medical_ai_held']+=1;continue
        packet={k:v for k,v in plan.items() if k not in {'review_id','crop_file'}}
        payload=load_crop(plan)
        verify_preparation(packet,payload,key)
        aid=analysis_key(source,packet['mapping'],model)
        existing=state.get(aid)
        if existing is None and not allow_send:
            fields['review_message']='支払額画像の匿名化検証済み。利用プラン確認前のためAI未送信。本人確定はしていません。'
            fields['provenance']={'status':'prepared_not_sent','analysis_id':aid,
                'crop_sha256':packet['mapping']['crop_sha256'],'local':packet['local_provenance']}
            state.publish(rid,source,fields);counts['medical_ai_held']+=1;continue
        if existing is None and counts['medical_ai_requests']>=LIMIT:
            counts['medical_ai_held']+=1;continue
        fresh=state.begin(aid,review_id=rid,source=source,mapping=packet['mapping'],model=model,prompt=PROMPT_SHA)
        if fresh:
            # Durable intent precedes the last source check and the only send.
            verify_source(source,item['folder_id'])
            counts['medical_ai_requests']+=1
            answer=send(packet,payload)
            state.complete(aid,answer.model_dump())
        else:
            answer=PaymentAnswer.model_validate(state.get(aid)['result'])
            counts['medical_ai_reused']+=1
        result,signals,policy,outcome=admit_answer(answer,payload=payload,mapping=packet['mapping'],
            model=model,identity_key=identity_key)
        candidate=answer.candidates[0] if answer.candidates else None
        if outcome.verdict=='AUTO_ADMITTED_FOR_AUTHORITY_EVALUATION' and candidate.label==packet['proof']['label']:
            fields['amount_yen']=candidate.amount_yen;counts['medical_ai_candidates']+=1
            fields['review_message']='画像AI/非AIの未確定候補。内容確認後「候補で医療費を確定」。不足・誤りだけH:Kへ入力してください。'
        else:
            counts['medical_ai_held']+=1
            fields['review_message']='金額の判読・実支払額の根拠が不足。原本を確認し、不足項目だけ入力してください。'
        fields['provenance']={'analysis_id':aid,'result_id':result.result_id,'model':model,'prompt':PROMPT_SHA,
            'amount_origin':'IMAGE_AI_CANDIDATE' if fields.get('amount_yen') else 'IMAGE_AI_ABSTENTION',
            'crop_sha256':packet['mapping']['crop_sha256'],'local':packet['local_provenance'],
            'admission':outcome.verdict,'region':candidate.region if candidate else None}
        verify_source(source,item['folder_id'])
        state.publish(rid,source,fields)
    return counts


def run_prepared(env,directory):
    if env.get('MEDICAL_DERIVED_AI_POLICY') not in {'prepare-only','reviewed-v1:paid','reviewed-v1:free'}:
        raise StateError('medical_service_terms_not_verified')
    settings,store,db,verify_source=open_context(env,True)
    from .settings import service_account_source
    path,info=service_account_source();info=info or json.loads(Path(path).read_bytes())
    identity_key=sha256(b'medical-candidate-evidence\0'+info['private_key'].encode()).digest()
    root=Path(directory).resolve();plans=json.loads((root/'medical-plan.json').read_bytes())
    def load_crop(plan):
        path=(root/plan['crop_file']).resolve()
        if path.parent!=root or path.name!=plan['review_id']+'.png':
            raise StateError('medical_crop_path_invalid')
        return path.read_bytes()
    sender_env=dict(env,GEMINI_MODEL=settings.gemini_model)
    return process_plans(plans,state=MedicalCandidateState(store),verify_source=verify_source,load_crop=load_crop,
        send=lambda p,b:send_derived(p,b,sender_env),model=settings.gemini_model,
        key=base64.b64decode(env['MEDICAL_CROP_ATTESTATION_KEY'],validate=True),identity_key=identity_key,
        allow_send=env['MEDICAL_DERIVED_AI_POLICY']!='prepare-only')
