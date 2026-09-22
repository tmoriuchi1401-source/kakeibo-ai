import io
import json
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from PIL import Image
from google.genai.errors import APIError

from app import gemini_probe as probe


def response(text='{"total":300}'):
    return SimpleNamespace(output_text=text)


def test_probe_uses_only_fixed_synthetic_inputs_and_four_spaced_calls():
    client = Mock()
    client.interactions.create.return_value = response()
    sleep = Mock(); emit = Mock()
    results = probe.run_probe(client, sleep=sleep, emit=emit)
    assert len(results) == 4
    assert all(r['outcome'] == 'valid' for r in results)
    assert [r['model'] for r in results] == [probe.MODELS[0]] * 2 + [probe.MODELS[1]] * 2
    assert [r['input'] for r in results] == ['text', 'image'] * 2
    assert sleep.call_count == 3
    assert all(c.args == (15,) for c in sleep.call_args_list)
    assert Image.open(io.BytesIO(probe.synthetic_image())).size == (720, 400)
    calls = client.interactions.create.call_args_list
    assert len(calls[0].kwargs['input']) == 1
    assert calls[1].kwargs['input'][1]['mime_type'] == 'image/png'
    assert calls[1].kwargs['input'][1] == calls[3].kwargs['input'][1]
    assert all(c.kwargs['response_format']['schema'] == probe.SCHEMA for c in calls)


@pytest.mark.parametrize('status', [401, 403, 429])
def test_probe_stops_immediately_on_auth_or_quota_and_never_logs_private_error(status):
    client = Mock()
    client.interactions.create.side_effect = APIError(status, {'error': {'message': 'PRIVATE_SECRET'}})
    messages = []
    results = probe.run_probe(client, sleep=Mock(), emit=messages.append)
    assert len(results) == client.interactions.create.call_count == 1
    assert results[0]['status'] == status
    assert 'PRIVATE_SECRET' not in ''.join(messages)


def test_503_compares_other_model_without_replaying_or_logging_server_message():
    client = Mock()
    client.interactions.create.side_effect = [
        APIError(503, {'error': {'message': 'PRIVATE_SECRET'}}),
        APIError(503, {'error': {'message': 'PRIVATE_SECRET'}}), response(), response(),
    ]
    messages = []
    results = probe.run_probe(client, sleep=Mock(), emit=messages.append)
    assert [r['status'] for r in results] == [503, 503, 200, 200]
    assert len(messages) == 4
    assert 'PRIVATE_SECRET' not in ''.join(messages)


@pytest.mark.parametrize('text', ['PRIVATE_SECRET', '{"total":301}', '{"total":300,"secret":"PRIVATE_SECRET"}', 'null'])
def test_response_is_validated_without_logging_its_contents(text):
    client = Mock(); client.interactions.create.return_value = response(text)
    messages = []
    results = probe.run_probe(client, sleep=Mock(), emit=messages.append)
    assert all(r['outcome'] == 'invalid_response' for r in results)
    assert 'PRIVATE_SECRET' not in ''.join(messages)
    assert all(set(json.loads(m)) == {'model', 'input', 'status', 'outcome', 'elapsed_seconds'} for m in messages)


def test_main_bounds_sdk_requests_and_uses_existing_key_only(monkeypatch):
    monkeypatch.setenv('GEMINI_API_KEY', 'synthetic-test-key')
    factory = Mock(); monkeypatch.setattr(probe.genai, 'Client', factory)
    run = Mock(return_value=[{'outcome': 'valid'}] * 4)
    monkeypatch.setattr(probe, 'run_probe', run)
    assert probe.main() == 0
    assert factory.call_args.kwargs == {
        'api_key': 'synthetic-test-key', 'http_options': {
            'api_version': 'v1', 'timeout': 90000, 'retry_options': {'attempts': 0},
        },
    }


def test_missing_key_never_constructs_client(monkeypatch, capsys):
    monkeypatch.delenv('GEMINI_API_KEY', raising=False)
    factory = Mock(); monkeypatch.setattr(probe.genai, 'Client', factory)
    assert probe.main() == 1
    factory.assert_not_called()
    assert json.loads(capsys.readouterr().out) == {'outcome': 'missing_key'}


@pytest.mark.parametrize('status,expected_calls', [(503, 4), (429, 1)])
def test_real_sdk_transport_never_replays_probe_requests(status, expected_calls, capsys, caplog):
    import httpx

    requests = []

    def handle(request):
        requests.append(request)
        return httpx.Response(status, json={'error': {
            'code': 'service_unavailable' if status == 503 else 'quota_exceeded',
            'message': 'SYNTHETIC_PRIVATE_ERROR',
        }})

    with httpx.Client(transport=httpx.MockTransport(handle)) as http_client:
        client = probe.create_probe_client('synthetic-test-key', httpx_client=http_client)
        messages = []
        results = probe.run_probe(client, sleep=Mock(), emit=messages.append)
        client.close()

    assert len(requests) == len(results) == expected_calls
    assert all(result['status'] == status for result in results)
    assert all(request.url.path == '/v1/interactions' for request in requests)
    assert all(request.extensions['timeout']['read'] == 90 for request in requests)
    assert [json.loads(request.content)['model'] for request in requests] == (
        [probe.MODELS[0], probe.MODELS[0], probe.MODELS[1], probe.MODELS[1]][:expected_calls]
    )
    output = capsys.readouterr()
    assert 'SYNTHETIC_PRIVATE_ERROR' not in ''.join(messages) + output.out + output.err + caplog.text
