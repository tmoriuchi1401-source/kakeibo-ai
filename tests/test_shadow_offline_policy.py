"""Offline visual policy only; no host fixture, network or archive extraction."""
from copy import deepcopy
import errno
import pytest

from shadow_visual_check import external_transport_results, validate_offline_policy, safe_url


def valid():
    return [
        dict(network_namespace='net:[2]',outer_namespace='net:[1]',loopback_only=True,external_interfaces=0,default_routes=0,
             egress_probes=[dict(protocol='TCP',ipv6=False,errno=errno.ENETUNREACH),dict(protocol='UDP',ipv6=False,errno=errno.ENETUNREACH),dict(protocol='TCP',ipv6=True,errno=errno.ENETUNREACH)]),
        dict(browser_network_namespace='net:[2]',cookie_scope_checked=True),
        dict(page_level_external=0,cdp_external=0,raw_netlog_deleted=True,proxy_external=1),
        dict(external_connect_attempts=1,external_connect_successes=0,external_connect_failures=1,unverifiable_external_connects=0),
        [dict(method='CONNECT',scheme='https',hostname='www.gstatic.com',port='443',path='unknown')],
        [safe_url('https://www.gstatic.com/ohttp_gateway/hpke_public_keys/v1/sbc_prod?key=synthetic')],
        dict(proxy_canary_matches=0,proxy_forwarded_connections=0,external_url_canary_matches=0,request_canary_matches=0),
    ]


def test_blocked_recorded_background_is_audited_only_with_complete_proof():
    args=valid();before=deepcopy(args)
    audit=validate_offline_policy(*args)
    assert audit['policy']=='kernel_blocked_synthetic_visual_only'
    assert audit['proxy_destinations']==[dict(host='www.gstatic.com',port='443',count=1,classification='previously_observed_Chrome_background_blocked')]
    assert args==before


@pytest.mark.parametrize('index,field,value',[
    (0,'network_namespace','net:[1]'),(0,'loopback_only',False),(0,'external_interfaces',1),(0,'default_routes',1),
    (0,'egress_probes',[]),(0,'egress_probes',[dict(protocol='TCP',ipv6=False,errno=errno.ENETUNREACH)]*3),
    (1,'browser_network_namespace','net:[1]'),(1,'cookie_scope_checked',False),
    (2,'page_level_external',1),(2,'cdp_external',1),(2,'raw_netlog_deleted',False),(2,'proxy_external',2),
    (3,'external_connect_successes',1),(3,'unverifiable_external_connects',1),(3,'external_connect_failures',0),
    (6,'proxy_canary_matches',1),(6,'proxy_forwarded_connections',1),(6,'external_url_canary_matches',1),(6,'request_canary_matches',1),
])
def test_bad_or_incomplete_evidence_fails_closed(index,field,value):
    args=valid();args[index][field]=value
    with pytest.raises(AssertionError):validate_offline_policy(*args)


@pytest.mark.parametrize('index,field',[(0,'network_namespace'),(1,'cookie_scope_checked'),(2,'raw_netlog_deleted'),(3,'external_connect_successes'),(6,'proxy_canary_matches')])
def test_missing_required_evidence_fails_closed(index,field):
    args=valid();del args[index][field]
    with pytest.raises((KeyError,AssertionError)):validate_offline_policy(*args)


@pytest.mark.parametrize('field,value',[('method','POST'),('scheme','http'),('hostname','unobserved.example.test'),('port','8443')])
def test_unexpected_proxy_request_rejected(field,value):
    args=valid();args[4][0][field]=value
    with pytest.raises(AssertionError):validate_offline_policy(*args)


@pytest.mark.parametrize('url',['https://unobserved.example.test/','https://www.google.com/unobserved','http://www.google.com/','https://www.google.com:8443/'])
def test_unexpected_url_rejected_without_network_permission(url):
    args=valid();args[5]=[safe_url(url)]
    with pytest.raises(AssertionError):validate_offline_policy(*args)


def log(kind='UDP_CONNECT',error=-109,closed=True,address='[2001:db8::1]:443'):
    events=[dict(type=1,phase=1,source=dict(id=7),params=dict(address=address))]
    if closed:events.append(dict(type=1,phase=2,source=dict(id=7),params={} if error is None else dict(net_error=error)))
    return dict(constants=dict(logEventTypes={kind:1},logEventPhase=dict(PHASE_BEGIN=1,PHASE_END=2)),events=events)


@pytest.mark.parametrize('kind',['UDP_CONNECT','TCP_CONNECT_ATTEMPT'])
def test_blocked_connect_has_explicit_failed_end(kind):
    assert external_transport_results(log(kind))==dict(external_connect_attempts=1,external_connect_successes=0,external_connect_failures=1,unverifiable_external_connects=0)


@pytest.mark.parametrize('error',[0,None])
def test_external_success_not_misclassified_as_blocked(error):
    result=external_transport_results(log(error=error));assert result['external_connect_successes']==1
    args=valid();args[3]=result
    with pytest.raises(AssertionError):validate_offline_policy(*args)


def test_unfinished_connect_is_unverifiable():
    result=external_transport_results(log(closed=False));assert result['unverifiable_external_connects']==1
    args=valid();args[3]=result
    with pytest.raises(AssertionError):validate_offline_policy(*args)


def test_unknown_endpoint_is_unverifiable():
    assert external_transport_results(log(address='not-an-ip:443'))['unverifiable_external_connects']==1


def test_loopback_is_not_external_success():
    assert external_transport_results(log(error=0,address='127.0.0.1:12345'))['external_connect_successes']==0


def test_missing_netlog_phase_schema_rejected():
    raw=log();raw['constants']['logEventPhase']={}
    with pytest.raises(AssertionError):external_transport_results(raw)
