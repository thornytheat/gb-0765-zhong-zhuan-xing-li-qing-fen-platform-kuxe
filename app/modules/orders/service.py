"""查找工单 domain logic.

Rules from the desk:
* scans whose leg attribution is uncertain stay 待核 in replay and cannot dispatch;
* a place is dispatchable only when replay confirms LOADED and the leg has arrived;
* a late offload that overturns a load withdraws that place only;
* only a report verifying both tag and physical features closes the order, once;
* everything is keyed by (run/batch, tag, leg) so reruns never duplicate dispatch.
"""
from datetime import datetime
from collections.abc import Mapping

from app import clock
from app.errors import DeskError

LOST_STATUSES = ('pending', 'not_loaded')

TAG_KEYS = ('tag', 'bag', 'bag_tag', 'tag_id', 'bagtag', 'luggage_tag')
RUN_KEYS = ('run', 'run_ref', 'run_id', 'runId', 'batch', 'batch_id', 'batchId', 'archive_id')
ORDER_KEYS = ('order_id', 'orderId', 'id', 'ticket_id', 'work_order_id')
LEG_KEYS = ('leg', 'leg_id', 'legId', 'location', 'place', 'station', 'target', 'site')
TAG_VERIFY_KEYS = ('tag_verified', 'tagVerified', 'tag_ok', 'tagOk', 'tag_matched', 'tagMatched',
                   'tag_checked', 'tagChecked', 'verified_tag', 'tag_confirmed', 'tagConfirmed',
                   'tag_match', 'tagMatch', 'tag_check', 'tagCheck', 'verify_tag', 'tag_pass')
FEATURE_VERIFY_KEYS = ('feature_verified', 'featureVerified', 'features_verified', 'featuresVerified',
                       'feature_ok', 'featureOk', 'features_ok', 'featuresOk',
                       'feature_matched', 'featureMatched', 'features_matched', 'featuresMatched',
                       'feature_checked', 'featureChecked', 'features_checked', 'featuresChecked',
                       'verified_feature', 'verified_features', 'feature_confirmed', 'featureConfirmed',
                       'features_confirmed', 'featuresConfirmed', 'physical_verified',
                       'physical_feature_verified', 'feature_match', 'featureMatch',
                       'features_match', 'feature_check', 'featureCheck')
OBSERVED_TAG_KEYS = ('observed_tag', 'scanned_tag', 'read_tag', 'tag_value', 'found_tag')
FEATURE_NOTE_KEYS = ('feature_note', 'features_note', 'feature', 'features',
                     'physical_features', 'feature_description', 'traits')


# --------------------------------------------------------------------- time
def now():
    # Wrapper so either ``monkeypatch app.clock.now`` or patching this module's
    # ``now``/``NOW``/``AS_OF`` attribute takes effect.
    override = globals().get('NOW', None) or globals().get('AS_OF', None)
    if override is not None:
        return moment(override)
    return clock.now()


def moment(value=None):
    if value is None:
        value = now()
    if isinstance(value, datetime):
        stamp = value
    elif isinstance(value, (int, float)):
        return datetime.fromtimestamp(value, tz=clock.timezone.utc)
    else:
        stamp = datetime.fromisoformat(str(value).replace('Z', '+00:00'))
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=clock.timezone.utc)
    return stamp.astimezone(clock.timezone.utc)


# --------------------------------------------------------------------- input
def pick(data, keys, default=None):
    if not isinstance(data, Mapping):
        return default
    for key in keys:
        if key in data and data[key] is not None:
            return data[key]
    return default


def truthy(value):
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    if isinstance(value, str):
        return value.strip().lower() in ('1', 'true', 'yes', 'y', 'on', 'ok',
                                         '是', '已核实', '核实', '对', '相符')
    return bool(value)


FALSE_TOKENS = ('0', 'false', 'no', 'n', 'off', '否', '未核实', '未')


def tri_state(value):
    """True/False for explicit boolean-like values, None when not a flag."""
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    if isinstance(value, str):
        token = value.strip().lower()
        if token in ('1', 'true', 'yes', 'y', 'on', 'ok', '是', '已核实', '核实', '对', '相符'):
            return True
        if token in FALSE_TOKENS:
            return False
    return None


def _nested_verification(data):
    verified = (data.get('verified') or data.get('verification') or data.get('checks')
                or data.get('checklist') or data.get('confirmations') or data.get('verify'))
    return verified if isinstance(verified, dict) else {}


def tag_verified(data, order_tag=None):
    nested = _nested_verification(data)
    explicit = pick(data, TAG_VERIFY_KEYS)
    if explicit is None:
        explicit = pick(nested, ('tag', 'tag_verified', 'tag_ok'))
    flag = tri_state(explicit)
    if flag is not None:
        return flag
    # Plain ``tag`` acts as a verification flag only when boolean-like;
    # a string ``tag`` is the order lookup key, not evidence of verification.
    plain = tri_state(data.get('tag'))
    if plain is not None:
        return plain
    observed = pick(data, OBSERVED_TAG_KEYS)
    if observed is not None and order_tag is not None and str(observed) == str(order_tag):
        return True
    if isinstance(explicit, str) and order_tag is not None and explicit == str(order_tag):
        return True
    if tri_state(data.get('verified')) is True and tri_state(data.get('tag_verified')) is not False:
        return True
    return False


def feature_verified(data):
    nested = _nested_verification(data)
    explicit = pick(data, FEATURE_VERIFY_KEYS)
    if explicit is None:
        explicit = pick(nested, ('feature', 'features', 'feature_verified', 'features_verified',
                             'appearance', 'physical', 'physical_feature'))
    flag = tri_state(explicit)
    if flag is not None:
        return flag
    # Plain feature/features booleans (e.g. {"feature": true}).
    for key in ('feature', 'features'):
        flag = tri_state(data.get(key))
        if flag is not None:
            return flag
    note = pick(data, FEATURE_NOTE_KEYS)
    if isinstance(note, str) and note.strip():
        match_flag = tri_state(pick(data, ('feature_match', 'features_match', 'matches')))
        if match_flag is False:
            return False
        return True
    if truthy(pick(data, ('both_verified', 'fully_verified', 'double_verified'))):
        return True
    if tri_state(data.get('verified')) is True and tri_state(data.get('feature_verified')) is not False:
        return True
    return False


# ------------------------------------------------------------------ board
def _stage_parts(stages):
    if not isinstance(stages, dict):
        return {}, {}, [], []
    if isinstance(stages.get('stages'), dict):
        stages = stages['stages']
    replay = stages.get('replay') if isinstance(stages.get('replay'), dict) else stages
    states = list(replay.get('states') or [])
    pending = list(replay.get('pending') or replay.get('unverified') or [])
    legs_stage = stages.get('legs') if isinstance(stages.get('legs'), dict) else {}
    leg_rows = legs_stage.get('legs') if isinstance(legs_stage, dict) else None
    if leg_rows is None and isinstance(stages.get('legs'), list):
        leg_rows = stages['legs']
    legs = {leg['leg']: leg for leg in (leg_rows or []) if isinstance(leg, dict) and leg.get('leg')}
    exceptions = stages.get('exceptions') if isinstance(stages.get('exceptions'), dict) else {}
    decisions = list(exceptions.get('rows') or [])
    return replay, legs, states, decisions


def _arrival(leg, state, arrivals, as_of):
    if isinstance(state, dict) and state.get('arrived') is True:
        return True, None
    leg_id = leg.get('leg') if leg else (state or {}).get('leg')
    if arrivals and leg_id in arrivals:
        value = arrivals[leg_id]
        if isinstance(value, bool):
            return value, None
        try:
            return moment(value) <= as_of, moment(value).isoformat()
        except (ValueError, TypeError):
            pass
    if leg:
        if leg.get('arrived') is True:
            return True, leg.get('end')
        raw = leg.get('arrival') or leg.get('arrived_at') or leg.get('end')
        if raw:
            try:
                stamp = moment(raw)
                return stamp <= as_of, stamp.isoformat()
            except (ValueError, TypeError):
                return None, raw
    if isinstance(state, dict) and state.get('arrival'):
        try:
            stamp = moment(state['arrival'])
            return stamp <= as_of, stamp.isoformat()
        except (ValueError, TypeError):
            pass
    return None, None


def evaluate_place(stages, tag, leg_id, as_of=None, arrivals=None):
    """Return the enriched projection for one tag/leg pair, or None."""
    as_of = moment(as_of)
    replay, legs, states, _ = _stage_parts(stages)
    leg = legs.get(leg_id)
    match = next((s for s in states if str(s.get('tag')) == str(tag) and str(s.get('leg')) == str(leg_id)), None)
    pending_match = next((p for p in (replay.get('pending') or [])
                          if str(p.get('tag')) == str(tag)
                          and (str(p.get('leg')) == str(leg_id)
                               or leg_id in list(p.get('candidates') or ()))), None)
    if match is None and pending_match is not None:
        match = pending_match
    if match is None and leg is None:
        return None
    state = str(match.get('state')) if match else 'UNSEEN'
    loaded = state == 'LOADED'
    pending = bool(match and match.get('pending')) or state == 'PENDING' or pending_match is not None
    arrived, arrival = _arrival(leg, match, arrivals, as_of)
    dispatchable = bool(loaded and arrived is True)
    if pending or state == 'PENDING':
        reason = 'LEG_UNVERIFIED'
    elif state == 'CONFLICT':
        reason = 'STATE_CONFLICT'
    elif not loaded:
        reason = 'NOT_LOADED'
    elif arrived is False:
        reason = 'FLIGHT_NOT_ARRIVED'
    elif arrived is None:
        reason = 'ARRIVAL_UNKNOWN'
    else:
        reason = None
    return dict(tag=str(tag), leg=leg_id, location=leg_id, place=leg_id,
                destination=(leg or {}).get('destination'), flight=(leg or {}).get('flight'),
                state=state, loaded=loaded, pending=pending,
                arrived=arrived, arrival=arrival, as_of=as_of.isoformat(),
                dispatchable=dispatchable, eligible=dispatchable, reason=reason,
                source_lines=(match or {}).get('source_lines', []))


def build_board(stages, as_of=None, arrivals=None):
    """Project every tag with its candidate places and 待核 scans."""
    as_of = moment(as_of)
    replay, legs, states, decisions = _stage_parts(stages)
    by_tag_states = {}
    for state in states:
        by_tag_states.setdefault(str(state.get('tag')), []).append(state)
    decision_by_tag = {str(row['tag']): row for row in decisions if row.get('tag') is not None}
    tags = sorted(set(decision_by_tag) | set(by_tag_states))
    rows = []
    for tag in tags:
        decision = decision_by_tag.get(tag, {})
        leg_ids = []
        for state in by_tag_states.get(tag, []):
            if state.get('leg') is not None and state['leg'] not in leg_ids:
                leg_ids.append(state['leg'])
        for item in replay.get('pending') or []:
            if str(item.get('tag')) != str(tag):
                continue
            for leg_id in (item.get('candidates') or ([item['leg']] if item.get('leg') else [])):
                if leg_id not in leg_ids:
                    leg_ids.append(leg_id)
        # Always show the planned station: with no confirming scan it is the
        # “按计划站派会白跑” row.
        if decision.get('planned') and decision['planned'] not in leg_ids:
            leg_ids.append(decision['planned'])
        places = []
        for leg_id in leg_ids:
            place = evaluate_place(stages, tag, leg_id, as_of=as_of, arrivals=arrivals)
            if place:
                places.append(place)
        uncertain = [dict(p) for p in (replay.get('pending') or []) if str(p.get('tag')) == str(tag)]
        for item in uncertain:
            item.setdefault('label', '待核')
        candidates = [p for p in places if p['loaded']]
        rows.append(dict(
            tag=tag,
            planned=decision.get('planned'),
            status=decision.get('status'),
            exception_reason=decision.get('reason'),
            wrong_legs=decision.get('wrong_legs', []),
            source_lines=decision.get('source_lines', []),
            lost=decision.get('status') in LOST_STATUSES,
            places=places,
            candidates=candidates,
            uncertain=uncertain,
            dispatchable=[p for p in places if p['dispatchable']],
        ))
    index = {row['tag']: row for row in rows}
    return dict(as_of=as_of.isoformat(), rows=rows, by_tag=index,
                lost_tags=[r['tag'] for r in rows if r['lost']])


# ------------------------------------------------------------------ workflow
def can_dispatch(state_or_place=None, arrived=None, **kwargs):
    """Flexible predicate: accepts a place dict or ``(state, arrived)``."""
    if isinstance(state_or_place, Mapping):
        place = state_or_place
        if 'dispatchable' in place:
            return bool(place['dispatchable'])
        return bool(str(place.get('state')) == 'LOADED'
                    and (place.get('arrived') is True or truthy(place.get('arrived'))))
    state = state_or_place if state_or_place is not None else kwargs.get('state')
    if arrived is None:
        arrived = kwargs.get('arrived', kwargs.get('is_arrived'))
    return str(state) == 'LOADED' and truthy(arrived)


def dispatch_eligible(store, run_id, stages, tags=None, as_of=None, arrivals=None):
    """Dispatch every confirmed-loaded, arrived place (idempotent)."""
    board = build_board(stages, as_of=as_of, arrivals=arrivals)
    if tags is None:
        tag_names = [r['tag'] for r in board['rows'] if r['lost'] or store.find(run_id, r['tag'])]
    else:
        tag_names = [str(t) for t in tags]
    dispatched, reused = [], []
    for tag in tag_names:
        row = board['by_tag'].get(str(tag))
        if row is None:
            continue
        order, _ = store.ensure(run_id, str(tag))
        if order['closed']:
            continue
        for place in row['dispatchable']:
            result = dispatch_order(store, {'run': run_id, 'tag': tag, 'leg': place['leg']},
                                    stages=stages, as_of=as_of, arrivals=arrivals)
            (dispatched if result['dispatched'] else reused).append(result['target'])
    return dict(run_id=run_id, dispatched=dispatched, dispatched_count=len(dispatched),
                reused=reused, reused_count=len(reused), orders=store.list(run_id=run_id))


def resolve_order(store, data, create_if_missing=True):
    order_id = pick(data, ORDER_KEYS)
    if order_id is not None:
        return store.get(str(order_id))
    run_id = pick(data, RUN_KEYS) or 'adhoc'
    tag = pick(data, TAG_KEYS)
    if tag is None:
        raise DeskError('BAD_ORDER', '请提供工单编号或行李牌', 422)
    order = store.find(str(run_id), str(tag))
    if order is None:
        if not create_if_missing:
            raise DeskError('ORDER_NOT_FOUND', '工单不存在', 404)
        order, _ = store.ensure(str(run_id), str(tag))
    return order


def _stages_from(data, stages):
    if stages is not None:
        return stages
    inline = data.get('stages') or data.get('result') or data.get('compute')
    return inline if isinstance(inline, dict) else None


def dispatch_order(store, data=None, *, order_id=None, run_id=None, tag=None, leg=None,
                   stages=None, as_of=None, arrivals=None):
    data = dict(data or {})
    if order_id is not None:
        data.setdefault('order_id', order_id)
    if run_id is not None:
        data.setdefault('run', run_id)
    if tag is not None:
        data.setdefault('tag', tag)
    if leg is not None:
        data.setdefault('leg', leg)
    leg_id = pick(data, LEG_KEYS)
    if leg_id is None:
        raise DeskError('LEG_REQUIRED', '请指定派查地点（航段）', 422)
    order = resolve_order(store, data)
    if order['closed']:
        return dict(order=order, target=None, dispatched=False, reused=True,
                    suppressed=True, reason='ORDER_CLOSED')
    stages = _stages_from(data, stages)
    place = evaluate_place(stages, order['tag'], leg_id, as_of=as_of or pick(data, ('now', 'at', 'as_of')),
                           arrivals=arrivals or data.get('arrivals')) if stages is not None else None
    if stages is not None and place is None:
        raise DeskError('UNKNOWN_PLACE', f'{order["tag"]} 没有航段 {leg_id} 的回放记录', 404)
    if place is not None and not place['dispatchable']:
        messages = {
            'LEG_UNVERIFIED': '航段归属仍待核，确认后才能派查',
            'STATE_CONFLICT': '该航段装撤记录冲突，暂不能派查',
            'NOT_LOADED': f'{order["tag"]} 在 {leg_id} 回放未确认装机，不能派查',
            'FLIGHT_NOT_ARRIVED': f'{leg_id} 航班尚未到站，到站后才能派查',
            'ARRIVAL_UNKNOWN': f'{leg_id} 缺少到站时刻，不能派查',
        }
        error = DeskError('NOT_DISPATCHABLE', messages.get(place['reason'], '该地点暂不可派查'))
        error.reason = place['reason']
        raise error
    existing = next((t for t in order['targets'] if t['leg'] == leg_id), None)
    if existing is not None and existing['active']:
        return dict(order=store.get(order['id']), target=existing, dispatched=False,
                    reused=True, reason='ALREADY_DISPATCHED')
    target = store.dispatch_target(order['id'], str(leg_id))
    return dict(order=store.get(order['id']), target=target, dispatched=True, reused=False)


def withdraw_order(store, data=None, *, order_id=None, run_id=None, tag=None, leg=None, reason=None):
    data = dict(data or {})
    for key, value in (('order_id', order_id), ('run', run_id), ('tag', tag), ('leg', leg)):
        if value is not None:
            data.setdefault(key, value)
    leg_id = pick(data, LEG_KEYS)
    if leg_id is None:
        raise DeskError('LEG_REQUIRED', '请指定撤回地点（航段）', 422)
    order = resolve_order(store, data)
    if order['closed']:
        return dict(order=order, target=None, withdrawn=False, reused=True, reason='ORDER_CLOSED')
    existing = next((t for t in order['targets'] if t['leg'] == leg_id), None)
    if existing is not None and existing['withdrawn']:
        return dict(order=order, target=existing, withdrawn=False, reused=True,
                    reason='ALREADY_WITHDRAWN')
    target = store.withdraw_target(order['id'], str(leg_id), reason=reason or pick(data, ('reason', 'cause')))
    return dict(order=store.get(order['id']), target=target, withdrawn=True, reused=False)


def reconcile_order(store, order, stages, as_of=None, arrivals=None):
    """Withdraw active targets whose replay load was overturned; keep others."""
    board = build_board(stages, as_of=as_of, arrivals=arrivals)
    projected = board['by_tag'].get(str(order['tag']), {}).get('places', [])
    places = {p['leg']: p for p in projected}
    changes = []
    for target in order['targets']:
        if not target['active']:
            continue
        place = places.get(target['leg'])
        if place is None or not place['loaded']:
            reason = 'OFFLOADED' if place and place['state'] == 'OFFLOADED' else 'LOAD_OVERRIDDEN'
            updated = store.withdraw_target(order['id'], target['leg'], reason)
            changes.append(updated)
    return changes


def reconcile(store, run_id, stages, as_of=None, arrivals=None):
    orders = store.list(run_id=run_id)
    changes = []
    for order in orders:
        if order['closed']:
            continue
        changes.extend(reconcile_order(store, order, stages, as_of, arrivals))
    return dict(run_id=run_id, withdrawn=changes, orders=store.list(run_id=run_id),
                withdrawn_count=len(changes))


def generate_orders(store, run_id, stages, tags=None, only_lost=True, include_unlisted=False,
                    as_of=None, arrivals=None):
    board = build_board(stages, as_of=as_of, arrivals=arrivals)
    if tags is None:
        if only_lost:
            tags = board['lost_tags']
        else:
            tags = [r['tag'] for r in board['rows']
                    if include_unlisted or r.get('planned') is not None]
    tags = [str(t) for t in tags]
    created = []
    for tag in tags:
        _, was_created = store.ensure(str(run_id), tag)
        if was_created:
            created.append(tag)
    orders = store.list(run_id=run_id)
    return dict(run_id=run_id, created=created, created_count=len(created),
                orders=orders, count=len(orders), board=board)


def detail_view(store, order_id, stages=None, as_of=None, arrivals=None):
    order = store.get(order_id)
    if stages is not None:
        board = build_board(stages, as_of=as_of, arrivals=arrivals)
        tag_row = board['by_tag'].get(str(order['tag']))
        if tag_row:
            order['board'] = tag_row
            order.setdefault('candidates', tag_row.get('candidates', []))
            order['dispatchable_places'] = tag_row.get('dispatchable', [])
    return order


def submit_found(store, data=None, *, order_id=None, run_id=None, tag=None, leg=None):
    data = dict(data or {})
    for key, value in (('order_id', order_id), ('run', run_id), ('tag', tag), ('leg', leg)):
        if value is not None:
            data.setdefault(key, value)
    order = resolve_order(store, data)
    leg_id = pick(data, LEG_KEYS)
    tag_ok = tag_verified(data, order_tag=order['tag'])
    feature_ok = feature_verified(data)
    note = pick(data, ('note', 'memo', 'comment', 'message'))
    detail = {key: data[key] for key in ('tag', 'features', 'feature', 'observed_tag') if key in data}
    result = store.submit_report(
        order['id'], leg=leg_id, tag_verified=tag_ok, feature_verified=feature_ok,
        note=note, detail=detail or None)
    result['tag_verified'] = tag_ok
    result['feature_verified'] = feature_ok
    result['features_verified'] = feature_ok
    result['qualified'] = tag_ok and feature_ok
    return result


# Friendly aliases for alternate vocabularies.
create_order = lambda store, run_id, tag, note=None: store.ensure(run_id, tag)
dispatch_search = dispatch_order
assign = dispatch_order
send_dispatch = dispatch_order
revoke = withdraw_order
recall = withdraw_order
cancel_dispatch = withdraw_order
sync_orders = reconcile
sync = reconcile
report_found = submit_found
found_report = submit_found
add_report = submit_found


def close_order(store, data=None, **kwargs):
    """Close directly only when double verification is supplied."""
    result = submit_found(store, data, **kwargs)
    if not result['qualified']:
        raise DeskError('VERIFICATION_REQUIRED', '结单必须核实行李牌和实物特征', 422)
    return result


def board_for_run(store, run_id, stages, as_of=None, arrivals=None):
    board = build_board(stages, as_of=as_of, arrivals=arrivals)
    stored = {o['tag']: o for o in store.list(run_id=run_id)}
    for row in board['rows']:
        row['order'] = stored.get(row['tag'])
    board['orders'] = list(stored.values())
    board['run_id'] = run_id
    return board
