"""Check the running detector/classifier pipeline with shared test frames."""
import json
import os
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BASE = os.environ.get('BAROMETER_URL', 'http://127.0.0.1:8321')

def main():
    with urllib.request.urlopen(BASE + '/api/health', timeout=10) as response:
        health = json.load(response)
    if health['state'] != 'ready':
        raise RuntimeError(health)
    print('Engine:', health['engine'], '\nModel:', health['emotion_key'])
    for name, count in [('none', 0), ('single', 1), ('multi', 3)]:
        body = (ROOT / 'web/test-frames' / f'{name}.jpg').read_bytes()
        for attempt in range(30):
            try:
                request = urllib.request.Request(BASE + '/api/infer', data=body)
                with urllib.request.urlopen(request, timeout=10) as response:
                    result = json.load(response)
                break
            except urllib.error.HTTPError as error:
                if error.code != 409 or attempt == 29:
                    raise
                time.sleep(.15)
        if result['count'] != count or bool(result['primary']) != (count == 1):
            raise RuntimeError(result)
        print('PASS', name, json.dumps({'count':result['count'], 'label':(result['primary'] or {}).get('label'), 'timing':result['timing']}, ensure_ascii=False))

if __name__ == '__main__':
    main()
