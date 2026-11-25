"""查找工单 rules: timely per-place dispatch, withdrawal, double verification,
close-once concurrency and batch-rerun idempotency."""
from concurrent.futures import ThreadPoolExecutor

import pytest

from app.input import sample
from app.modules.orders.service import build_board, can_dispatch
from app.pipeline import compute


# ------------------------------------------------------------ replay 待核
def test_ambiguous_scan_stays_pending_in_replay(dirty):
    pending = [r for r in dirty['replay']['pending'] if r['tag'] == '000103']
    assert len(pending) == 1
    row = pending[0]
    assert str(row['state']) == 'PENDING'
    assert row['pending'] is True and row['label'] == '待核'
    assert row['candidates'] == ['HX-41', 'HX-42']
    # Also represented once in terminal states, without a resolved leg.
    states = [s for s in dirty['replay']['states'] if s['tag'] == '000103']
    assert len(states) == 1 and str(states[0]['state']) == 'PENDING'


def test_pending_candidate_places_are_not_dispatchable(dirty):
    board = build_board(dirty)
    row = board['by_tag']['000103']
    places = {p['leg']: p for p in row['places']}
    assert set(places) == {'HX-41', 'HX-42'}
    assert all(p['reason'] == 'LEG_UNVERIFIED' and not p['dispatchable'] for p in places.values())
    assert can_dispatch('LOADED', True) and not can_dispatch('PENDING', True)
    assert not can_dispatch({'state': 'LOADED', 'arrived': False})


def test_loaded_and_arrived_makes_places_dispatchable(dirty):
    board = build_board(dirty)
    row = board['by_tag']['000108']
    places = {p['leg']: p for p in row['places']}
    assert places['JL-08']['dispatchable'] and places['NX-02']['dispatchable']


def test_dispatch_follows_evidence_not_planned_station(dirty):
    board = build_board(dirty)
    row = board['by_tag']['000105']  # planned NX-02, actually loaded NX-01
    places = {p['leg']: p for p in row['places']}
    assert places['NX-01']['loaded'] and places['NX-01']['dispatchable']
    assert not places['NX-02']['loaded']


# ------------------------------------------------------------ page
def test_orders_page_renders(desk, saved):
    desk.post('/api/orders/generate', json={'run': saved['id']})
    result = desk.get(f"/desk/orders?run={saved['id']}")
    text = result.get_data(as_text=True)
    assert result.status_code == 200
    assert '查找工单' in text and '提交找到回报' in text
    assert '000108' in text and '派查' in text


def test_orders_nav_present(desk):
    text = desk.get('/desk/orders').get_data(as_text=True)
    assert '查找工单' in text and '/desk/orders' in text


# ------------------------------------------------------------ API gating
def test_ambiguous_leg_cannot_dispatch(desk, saved):
    resp = desk.post('/api/orders/dispatch', json={'run': saved['id'], 'tag': '000103', 'leg': 'HX-41'})
    assert resp.status_code == 422
    assert resp.json['reason'] == 'LEG_UNVERIFIED'


def test_arrival_gate(desk, saved):
    early = desk.post('/api/orders/dispatch', json={
        'run': saved['id'], 'tag': '000110', 'leg': 'HX-42',
        'now': '2026-11-20T17:00:00Z'})
    assert early.status_code == 422 and early.json['reason'] == 'FLIGHT_NOT_ARRIVED'
    ok = desk.post('/api/orders/dispatch', json={
        'run': saved['id'], 'tag': '000110', 'leg': 'HX-42',
        'now': '2026-11-21T12:00:00+08:00'})
    assert ok.status_code == 200 and ok.json['dispatched'] is True


def test_dispatch_two_places_idempotent(desk, saved):
    payload = {'run': saved['id'], 'tag': '000108'}
    a = desk.post('/api/orders/dispatch', json={**payload, 'leg': 'JL-08'}).json
    b = desk.post('/api/orders/dispatch', json={**payload, 'leg': 'NX-02'}).json
    again = desk.post('/api/orders/dispatch', json={**payload, 'leg': 'JL-08'}).json
    assert a['dispatched'] and b['dispatched']
    assert again['dispatched'] is False and again['reused'] is True
    oid = a['order']['id']
    order = desk.get(f'/api/orders/{oid}').json
    assert {t['leg'] for t in order['targets']} == {'JL-08', 'NX-02'}


# ------------------------------------------------------------ withdrawal
def test_late_offload_withdraws_only_that_place(desk, saved):
    import copy
    rid = saved['id']
    oid = desk.post('/api/orders/dispatch', json={'run': rid, 'tag': '000108', 'leg': 'JL-08'}).json['order']['id']
    desk.post(f'/api/orders/{oid}/dispatch', json={'leg': 'NX-02'})
    _, result = desk.application.extensions['transfer_archive'].get(rid)
    stages = copy.deepcopy(result['stages'])
    for state in stages['replay']['states']:
        if state['tag'] == '000108' and state['leg'] == 'JL-08':
            state['state'] = 'OFFLOADED'
    resp = desk.post(f'/api/orders/{oid}/reconcile', json={'stages': stages}).json
    assert [t['leg'] for t in resp['withdrawn']] == ['JL-08']
    legs = {t['leg']: t['status'] for t in resp['order']['targets']}
    assert legs == {'JL-08': 'withdrawn', 'NX-02': 'dispatched'}
    # The other place can still be worked and close the order.
    found = desk.post(f'/api/orders/{oid}/found', json={
        'leg': 'NX-02', 'tag_verified': True, 'feature_verified': True}).json
    assert found['closed'] is True


def test_withdraw_endpoint_keeps_other_place(desk, saved):
    rid = saved['id']
    oid = desk.post('/api/orders/dispatch', json={'run': rid, 'tag': '000108', 'leg': 'JL-08'}).json['order']['id']
    desk.post(f'/api/orders/{oid}/dispatch', json={'leg': 'NX-02'})
    resp = desk.post(f'/api/orders/{oid}/withdraw', json={'leg': 'JL-08'})
    assert resp.status_code == 200 and resp.json['withdrawn'] is True
    statuses = {t['leg']: t['status'] for t in desk.get(f'/api/orders/{oid}').json['targets']}
    assert statuses == {'JL-08': 'withdrawn', 'NX-02': 'dispatched'}


# ------------------------------------------------------------ close rules
def test_report_needs_both_verifications(desk, saved):
    rid = saved['id']
    oid = desk.post('/api/orders', json={'run': rid, 'tag': '000108'}).json['id']
    for body in (
        {'tag_verified': True},
        {'feature_verified': True},
        {'tag_verified': True, 'feature_verified': False},
        {'tag_verified': False, 'feature_verified': True},
    ):
        resp = desk.post(f'/api/orders/{oid}/found', json=body).json
        assert resp['closed'] is False and resp['status'] == 'open'
    order = desk.get(f'/api/orders/{oid}').json
    assert order['closed'] is False and len(order['reports']) == 4


def test_order_closes_once_under_concurrent_found_reports(desk, saved):
    rid = saved['id']
    oid = desk.post('/api/orders/dispatch', json={'run': rid, 'tag': '000108', 'leg': 'JL-08'}).json['order']['id']
    desk.post(f'/api/orders/{oid}/dispatch', json={'leg': 'NX-02'})

    def found(leg):
        client = desk.application.test_client()
        resp = client.post(f'/api/orders/{oid}/found', json={
            'leg': leg, 'tag_verified': True, 'features_verified': True})
        assert resp.status_code == 200
        return resp.json

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(found, ['JL-08', 'NX-02']))
    assert sorted(r['closed'] for r in results) == [False, True]
    order = desk.get(f'/api/orders/{oid}').json
    assert order['status'] == 'closed' and order['closed'] is True
    assert sum(r['closes_order'] for r in order['reports']) == 1


def test_unqualified_concurrent_reports_do_not_close(desk, saved):
    rid = saved['id']
    oid = desk.post('/api/orders', json={'run': rid, 'tag': '000108'}).json['id']

    def found(_):
        client = desk.application.test_client()
        return client.post(f'/api/orders/{oid}/found',
                           json={'tag_verified': True, 'feature_verified': False}).json

    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(found, range(4)))
    assert not any(r['closed'] for r in results)
    assert desk.get(f'/api/orders/{oid}').json['status'] == 'open'


# ------------------------------------------------------------ rerun idempotency
def test_rerun_old_batch_does_not_redispatch_closed_order(desk, saved):
    rid = saved['id']
    oid = desk.post('/api/orders/dispatch', json={'run': rid, 'tag': '000108', 'leg': 'JL-08'}).json['order']['id']
    desk.post(f'/api/orders/{oid}/found', json={'tag_verified': True, 'feature_verified': True})
    assert desk.post(f'/api/archive/{rid}/rerun').json['reused'] is True
    # Re-generating and re-dispatching against the old batch adds nothing.
    desk.post('/api/orders/generate', json={'run': rid})
    resp = desk.post('/api/orders/dispatch', json={'run': rid, 'tag': '000108', 'leg': 'JL-08'}).json
    assert resp['dispatched'] is False and resp['reused'] is True
    order = desk.get(f'/api/orders/{oid}').json
    assert order['status'] == 'closed' and len(order['targets']) == 1
    assert desk.get('/api/orders?run=' + rid).json['count'] == 9


def test_generate_is_idempotent(desk, saved):
    rid = saved['id']
    first = desk.post('/api/orders/generate', json={'run': rid}).json
    second = desk.post('/api/orders/generate', json={'run': rid}).json
    assert first['count'] == second['count'] == 9
    assert second['created_count'] == 0


def test_create_same_tag_returns_same_order(desk, saved):
    payload = {'run': saved['id'], 'tag': '000108'}
    a = desk.post('/api/orders', json=payload).json
    b = desk.post('/api/orders', json=payload).json
    assert a['id'] == b['id'] and a['created'] is True and b['created'] is False


def test_generate_all_manifest_bags(desk, saved):
    result = desk.post('/api/orders/generate', json={'run': saved['id'], 'all': True}).json
    assert result['count'] == 12  # manifest bags, unlisted 009999 excluded


def test_detail_carries_live_candidates(desk, saved):
    oid = desk.post('/api/orders', json={'run': saved['id'], 'tag': '000108'}).json['id']
    detail = desk.get(f'/api/orders/{oid}').json
    legs = {c['leg'] for c in detail['candidates']}
    assert legs == {'JL-08', 'NX-02'}


# ------------------------------------------------------------ forms / page
def test_page_form_workflow(desk, saved):
    rid = saved['id']
    desk.post('/desk/orders/generate', data={'run_ref': rid})
    resp = desk.post('/desk/orders/dispatch',
                     data={'run_ref': rid, 'tag': '000108', 'leg': 'JL-08'})
    assert resp.status_code == 303
    page = desk.get(f'/desk/orders?run={rid}').get_data(as_text=True)
    assert '已派人查件' in page and '撤回该处' in page
    oid = next(o['id'] for o in desk.get(f'/api/orders?run={rid}').json['orders'] if o['tag'] == '000108')
    desk.post('/desk/orders/report', data={'run_ref': rid, 'order_id': oid, 'tag_verified': '1'})
    resp = desk.post('/desk/orders/report', data={
        'run_ref': rid, 'order_id': oid, 'tag_verified': '1', 'feature_verified': '1'})
    assert resp.status_code == 303
    page = desk.get(resp.headers['Location']).get_data(as_text=True)
    assert '整张工单已结单' in page


def test_sample_preview_page_is_actionable_without_archive(desk):
    page = desk.get('/desk/orders').get_data(as_text=True)
    assert 'sample:dirty' in page
    resp = desk.post('/desk/orders/dispatch', data={
        'run_ref': 'sample:dirty', 'sample': 'dirty', 'tag': '000108', 'leg': 'JL-08'})
    assert resp.status_code == 303
    follow = desk.get('/desk/orders?run=sample:dirty&sample=dirty').get_data(as_text=True)
    assert '已派人查件' in follow


# ------------------------------------------------------------ clock patch
def test_clock_now_monkeypatch_gate(desk, saved, monkeypatch):
    from app import clock
    from datetime import datetime, timezone
    fixed = datetime(2026, 11, 20, 12, 0, tzinfo=timezone.utc)
    monkeypatch.setattr(clock, 'now', lambda: fixed)
    resp = desk.post('/api/orders/dispatch', json={'run': saved['id'], 'tag': '000110', 'leg': 'HX-42'})
    assert resp.status_code == 422 and resp.json['reason'] == 'FLIGHT_NOT_ARRIVED'


def test_direct_store_close_once(tmp_path):
    from app.modules.orders.store import OrderStore
    store = OrderStore(tmp_path / 'o.sqlite3')
    order, _ = store.create('run-1', '000108')
    outcomes = []
    with ThreadPoolExecutor(max_workers=4) as pool:
        outcomes = list(pool.map(
            lambda i: store.submit_report(order['id'], leg=f'L{i}',
                                          tag_verified=True, feature_verified=True)['closed'],
            range(4)))
    assert sorted(outcomes) == [False, False, False, True]
    assert store.get(order['id'])['closed'] is True
