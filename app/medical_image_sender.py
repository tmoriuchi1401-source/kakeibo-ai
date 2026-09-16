"""Isolated Gemini sender: stdin is only derived pixels and their attestation."""
import base64
import json
import os
import sys

from .gemini_ai import GeminiAI
from .medical_image_candidate import request_payment


def execute(packet,env):
    if set(packet)!={'png','proof'}:raise ValueError('sender_input_not_minimal')
    from .medical_auto_posting import AUTO_POLICIES
    if env.get('MEDICAL_DERIVED_AI_POLICY') not in {'reviewed-v1:paid','reviewed-v1:free'}|AUTO_POLICIES:
        raise ValueError('medical_service_terms_not_verified')
    if any(env.get(k) for k in ('GOOGLE_SERVICE_ACCOUNT_JSON','GOOGLE_SERVICE_ACCOUNT_FILE','GOOGLE_APPLICATION_CREDENTIALS',
            'GOOGLE_GMAIL_TOKEN_JSON','SPREADSHEET_ID','RECEIPT_DRIVE_FOLDER_ID')):
        raise ValueError('medical_sender_credentials_not_isolated')
    key=base64.b64decode(env['MEDICAL_CROP_ATTESTATION_KEY'],validate=True)
    payload=base64.b64decode(packet['png'],validate=True)
    # Validate before even constructing a network client.
    from .medical_image_candidate import verify_crop
    verify_crop(payload,packet['proof'],key)
    ai=GeminiAI(env['GEMINI_API_KEY'],env['GEMINI_MODEL'],request_attempts=1)
    try:return request_payment(ai.client,ai.model,payload,packet['proof'],key).model_dump()
    finally:ai.client.close()


def main():
    try:
        packet=json.loads(sys.stdin.read(8_000_000))
        print(json.dumps(execute(packet,dict(os.environ)),ensure_ascii=True))
    except Exception:
        # No provider response, raw image/text, key or prompt is printed.
        print(json.dumps({'error':'medical_image_response_unknown'}));raise SystemExit(1)


if __name__=='__main__':main()
