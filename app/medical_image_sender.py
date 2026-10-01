"""Retired Medical image sender; legacy invocations fail closed."""
import json
import os
import sys

def execute(packet,env):
    # Medical is human-confirmed. Legacy CLI/config cannot send derived pixels.
    raise ValueError('medical_human_confirmation_required')


def main():
    try:
        packet=json.loads(sys.stdin.read(8_000_000))
        print(json.dumps(execute(packet,dict(os.environ)),ensure_ascii=True))
    except Exception:
        # No provider response, raw image/text, key or prompt is printed.
        print(json.dumps({'error':'medical_image_response_unknown'}));raise SystemExit(1)


if __name__=='__main__':main()
