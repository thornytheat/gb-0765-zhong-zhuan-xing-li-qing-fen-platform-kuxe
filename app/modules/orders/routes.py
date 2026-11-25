from flask import Blueprint, current_app, jsonify, redirect, request

from app.config import VERSION
from app.errors import DeskError
from app.input import sample, submission
from app.manifest import fingerprint
from app.pipeline import compute
from . import service
from .store import OrderStore

bp = Blueprint('orders', __name__)


def book():
    return current_app.extensions['order_book']


def archive_store():
    return current_app.extensions['transfer_archive']


def store_for(app):
    return app.extensions['order_book']


# ------------------------------------------------------------------ source
def resolve_source(values, need=False):
    """Return (run_ref, stages), resolving archived runs or named samples."""
    values = values or {}
    run_ref = service.pick(values, service.RUN_KEYS + ('run_id',))
    if run_ref is not None:
        run_ref = str(run_ref)
        if run_ref.startswith('sample:'):
            name = run_ref.split(':', 1)[1]
            return run_ref, compute(sample(name))
        _, result = archive_store().get(run_ref)
        return run_ref, result['stages']
    name = values.get('sample') or values.get('selected')
    if name:
        return f'sample:{name}', compute(sample(name))
    if isinstance(values.get('files'), dict):
        files = submission({'files': values['files']})
        stages = compute(files)
        ref = values.get('run') or values.get('run_id') or ('draft-' + fingerprint(files)[:16])
        return str(ref), stages
    if need:
        raise DeskError('RUN_REQUIRED', '请先选择样例或归档批次', 400)
    return None, None


def _as_of(values):
    return service.pick(values, ('now', 'at', 'as_of', 'asof', 'time', 'clock'))


def _arrivals(values):
    arrivals = values.get('arrivals') or values.get('arrival')
    return arrivals if isinstance(arrivals, dict) else None


def _json_payload():
    body = request.get_json(silent=True)
    data = dict(body) if isinstance(body, dict) else {}
    # Also accept classic form posts and query parameters on /api routes.
    if request.form:
        for key, value in request.form.items():
            data.setdefault(key, value)
    for key, value in request.args.items():
        data.setdefault(key, value)
    return data


# ------------------------------------------------------------------ JSON API
@bp.get('/api/orders')
@bp.get('/api/work-orders')
@bp.get('/api/work_orders')
@bp.get('/api/tickets')
@bp.get('/api/tracing/orders')
@bp.get('/api/traces')
@bp.get('/api/lost-baggage')
def list_orders():
    run_ref = request.args.get('run') or request.args.get('run_id')
    tag = request.args.get('tag')
    if run_ref:
        run_ref, stages = resolve_source({'run': run_ref, 'sample': request.args.get('sample')})
        view = service.board_for_run(book(), run_ref, stages, as_of=request.args.get('now'))
        orders = view['orders']
        return jsonify(count=len(orders), orders=orders, rows=orders, board=view)
    orders = book().list(tag=tag)
    return jsonify(count=len(orders), orders=orders, rows=orders)


@bp.post('/api/orders')
@bp.post('/api/work-orders')
@bp.post('/api/work_orders')
@bp.post('/api/tickets')
@bp.post('/api/tracing/orders')
@bp.post('/api/traces')
@bp.post('/api/lost-baggage')
def create_or_act():
    data = _json_payload()
    action = data.get('action') or request.args.get('action')
    if action in ('dispatch', 'dispatch_search', 'assign'):
        return dispatch(data)
    if action in ('withdraw', 'recall', 'revoke', 'cancel'):
        return withdraw(data)
    if action in ('report', 'found', 'close', 'submit_found'):
        return report(data)
    run_ref, stages = resolve_source(data, need=True)
    tag = service.pick(data, service.TAG_KEYS)
    if tag is not None and not data.get('all') and not data.get('generate'):
        order, created = book().ensure(run_ref, str(tag))
        if stages is not None:
            order['board'] = service.build_board(stages, as_of=_as_of(data))['by_tag'].get(str(tag))
        return jsonify(order=order, created=created, id=order['id'], **{'reused': not created})
    tags = data.get('tags')
    result = service.generate_orders(
        book(), run_ref, stages, tags=tags,
        only_lost=not data.get('all'), as_of=_as_of(data), arrivals=_arrivals(data))
    return jsonify(result)


@bp.get('/api/orders/board')
@bp.get('/api/orders/candidates')
def board_api():
    data = dict(request.args)
    run_ref, stages = resolve_source(data, need=True)
    view = service.board_for_run(book(), run_ref, stages, as_of=request.args.get('now'))
    return jsonify(view)


@bp.post('/api/orders/generate')
@bp.post('/api/orders/auto')
def generate_api():
    data = _json_payload()
    run_ref, stages = resolve_source(data, need=True)
    result = service.generate_orders(
        book(), run_ref, stages, tags=data.get('tags'), only_lost=not data.get('all'),
        as_of=_as_of(data), arrivals=_arrivals(data))
    return jsonify(result)


def dispatch(data, order_id=None):
    if order_id:
        data = dict(data, order_id=order_id)
    run_ref, stages = resolve_source(data)
    if stages is None and (data.get('stages') or data.get('states')):
        stages = data
    leg_id = service.pick(data, service.LEG_KEYS)
    if not leg_id:
        # No place specified: dispatch every place the replay confirms and
        # that has arrived, without waiting for the other candidates.
        order = service.resolve_order(book(), data)
        if stages is None and order['run_id'] and order['run_id'] != 'adhoc':
            try:
                _, stages = resolve_source({'run': order['run_id']})
            except DeskError:
                stages = None
        if stages is None:
            raise DeskError('LEG_REQUIRED', '请指定派查地点（航段）或提供批次回放', 422)
        bulk = service.dispatch_eligible(
            book(), order['run_id'], stages, tags=[order['tag']],
            as_of=_as_of(data), arrivals=_arrivals(data))
        bulk['order'] = book().get(order['id'])
        new_targets = bulk['dispatched']
        bulk['target'] = new_targets[0] if new_targets else None
        bulk['dispatched'] = bulk['dispatched_count'] > 0
        return jsonify(_with_top_level(bulk))
    result = service.dispatch_order(book(), data, stages=stages, as_of=_as_of(data),
                                    arrivals=_arrivals(data))
    return jsonify(_with_top_level(result))


def withdraw(data, order_id=None):
    if order_id:
        data = dict(data, order_id=order_id)
    result = service.withdraw_order(book(), data)
    return jsonify(_with_top_level(result))


def report(data, order_id=None):
    if order_id:
        data = dict(data, order_id=order_id)
    result = service.submit_found(book(), data)
    return jsonify(_with_top_level(result))


def _with_top_level(result):
    order = result.get('order') or {}
    result.setdefault('id', order.get('id'))
    result.setdefault('order_id', order.get('id'))
    result.setdefault('status', order.get('status'))
    result.setdefault('work_order', order)
    result.setdefault('ticket', order)
    result.setdefault('closed', bool(order.get('closed')) if 'closed' in order else result.get('closed'))
    return result


@bp.post('/api/orders/dispatch')
@bp.post('/api/work-orders/dispatch')
def dispatch_body():
    return dispatch(_json_payload())


@bp.post('/api/dispatch')
@bp.post('/api/dispatch/search')
def dispatch_top_level():
    return dispatch(_json_payload())


@bp.post('/api/orders/dispatch-eligible')
@bp.post('/api/orders/auto-dispatch')
@bp.post('/api/orders/dispatch-all')
def dispatch_eligible_api():
    data = _json_payload()
    run_ref, stages = resolve_source(data, need=True)
    result = service.dispatch_eligible(
        book(), run_ref, stages, tags=data.get('tags'),
        as_of=_as_of(data), arrivals=_arrivals(data))
    return jsonify(result)


@bp.post('/api/orders/withdraw')
@bp.post('/api/orders/recall')
def withdraw_body():
    return withdraw(_json_payload())


@bp.post('/api/orders/report')
@bp.post('/api/orders/found')
@bp.post('/api/orders/close')
def report_body():
    return report(_json_payload())


@bp.post('/api/found')
@bp.post('/api/report')
@bp.post('/api/close')
def report_top_level():
    return report(_json_payload())


@bp.post('/api/withdraw')
@bp.post('/api/recall')
def withdraw_top_level():
    return withdraw(_json_payload())


@bp.post('/api/orders/reconcile')
@bp.post('/api/orders/sync')
def reconcile_body():
    data = _json_payload()
    run_ref, stages = resolve_source(data)
    if stages is None:
        inline = data.get('stages') or data.get('result')
        if isinstance(inline, dict):
            stages = inline.get('stages', inline)
    if stages is None or run_ref is None:
        raise DeskError('RUN_REQUIRED', '请提供批次与回放结果', 400)
    result = service.reconcile(book(), run_ref, stages, as_of=_as_of(data))
    return jsonify(result)


@bp.get('/api/orders/<order_id>')
@bp.get('/api/work-orders/<order_id>')
@bp.get('/api/order/<order_id>')
@bp.get('/api/tickets/<order_id>')
def detail(order_id):
    order = book().get(order_id)
    stages = None
    try:
        run_ref, resolved_stages = resolve_source({'run': order['run_id']})
        stages = resolved_stages
    except DeskError:
        stages = None
    return jsonify(service.detail_view(book(), order_id, stages=stages, as_of=request.args.get('now')))


@bp.post('/api/orders/<order_id>/dispatch')
@bp.post('/api/orders/<order_id>/assign')
@bp.post('/api/work-orders/<order_id>/dispatch')
@bp.post('/api/order/<order_id>/dispatch')
def dispatch_item(order_id):
    return dispatch(_json_payload() or {}, order_id)


@bp.post('/api/orders/<order_id>/dispatch-eligible')
@bp.post('/api/orders/<order_id>/dispatch-all')
def dispatch_item_eligible(order_id):
    order = book().get(order_id)
    data = _json_payload() or {}
    run_ref, stages = resolve_source(dict(data, run=order['run_id']))
    result = service.dispatch_eligible(book(), run_ref, stages, tags=[order['tag']],
                                       as_of=_as_of(data))
    return jsonify(result)


@bp.post('/api/orders/<order_id>/withdraw')
@bp.post('/api/orders/<order_id>/recall')
@bp.post('/api/orders/<order_id>/revoke')
@bp.post('/api/orders/<order_id>/cancel')
@bp.post('/api/order/<order_id>/withdraw')
def withdraw_item(order_id):
    return withdraw(_json_payload() or {}, order_id)


@bp.post('/api/orders/<order_id>/reports')
@bp.post('/api/orders/<order_id>/report')
@bp.post('/api/orders/<order_id>/found')
@bp.post('/api/orders/<order_id>/close')
@bp.post('/api/orders/<order_id>/complete')
@bp.post('/api/orders/<order_id>/settle')
@bp.post('/api/tickets/<order_id>/found')
def report_item(order_id):
    return report(_json_payload() or {}, order_id)


@bp.post('/api/orders/<order_id>/reconcile')
def reconcile_item(order_id):
    data = _json_payload() or {}
    order = book().get(order_id)
    stages = data.get('stages') or data.get('result')
    if isinstance(stages, dict) and 'stages' in stages:
        stages = stages['stages']
    if stages is None:
        _, full = archive_store().get(order['run_id']) if not order['run_id'].startswith('sample:') else (
            None, dict(stages=compute(sample(order['run_id'].split(':', 1)[1]))))
        stages = full['stages'] if 'stages' in full else full
    changes = service.reconcile_order(book(), order, stages, as_of=_as_of(data))
    return jsonify(order=book().get(order_id), withdrawn=changes, withdrawn_count=len(changes))


# Run-scoped aliases: /api/runs/<run>/orders... and /api/archive/<run>/orders...
@bp.get('/api/runs/<run_id>/orders')
@bp.get('/api/archive/<run_id>/orders')
def run_list(run_id):
    _, stages = resolve_source({'run': run_id})
    view = service.board_for_run(book(), run_id, stages)
    return jsonify(count=len(view['orders']), orders=view['orders'], rows=view['orders'], board=view)


@bp.post('/api/runs/<run_id>/orders')
@bp.post('/api/archive/<run_id>/orders')
def run_create(run_id):
    data = dict(_json_payload(), run=run_id)
    return _run_create(data)


def _run_create(data):
    run_ref, stages = resolve_source(data, need=True)
    tag = service.pick(data, service.TAG_KEYS)
    if tag is not None:
        order, created = book().ensure(run_ref, str(tag))
        return jsonify(order=order, created=created, id=order['id'], reused=not created)
    result = service.generate_orders(book(), run_ref, stages, tags=data.get('tags'),
                                    only_lost=not data.get('all'))
    return jsonify(result)


@bp.post('/api/runs/<run_id>/orders/dispatch')
@bp.post('/api/archive/<run_id>/orders/dispatch')
def run_dispatch(run_id):
    return dispatch(dict(_json_payload(), run=run_id))


@bp.post('/api/runs/<run_id>/orders/report')
@bp.post('/api/runs/<run_id>/orders/found')
@bp.post('/api/archive/<run_id>/orders/found')
def run_report(run_id):
    return report(dict(_json_payload(), run=run_id))


# ------------------------------------------------------------------ UI forms
def _redirect_back(form, notice=None, kind='ok'):
    from urllib.parse import urlencode
    run_ref = form.get('run_ref') or form.get('run')
    sample_name = form.get('sample', 'dirty')
    params = {}
    if run_ref:
        params['run'] = run_ref
    if sample_name:
        params['sample'] = sample_name
    if notice:
        params['emsg' if kind == 'err' else 'notice'] = notice
    url = '/desk/orders' + ('?' + urlencode(params) if params else '')
    return redirect(url, code=303)


@bp.post('/desk/orders/generate')
def ui_generate():
    form = request.form
    try:
        run_ref, stages = resolve_source(form, need=True)
        service.generate_orders(book(), run_ref, stages, only_lost=form.get('all') != '1')
        return _redirect_back(form, '已按回放证据生成查找工单')
    except DeskError as exc:
        return _redirect_back(form, str(exc), kind='err')


@bp.post('/desk/orders/create')
def ui_create():
    form = request.form
    try:
        run_ref, _ = resolve_source(form, need=True)
        tag = form.get('tag')
        if not tag:
            raise DeskError('BAD_ORDER', '缺少行李牌')
        book().ensure(run_ref, str(tag))
        return _redirect_back(form, f'行李牌 {tag} 的工单已建立')
    except DeskError as exc:
        return _redirect_back(form, str(exc), kind='err')


@bp.post('/desk/orders/dispatch')
def ui_dispatch():
    form = request.form
    try:
        run_ref, stages = resolve_source(form)
        service.dispatch_order(book(), form.to_dict(flat=True), stages=stages)
        return _redirect_back(form, '已通知该处派人查件')
    except DeskError as exc:
        return _redirect_back(form, str(exc), kind='err')


@bp.post('/desk/orders/withdraw')
def ui_withdraw():
    form = request.form
    try:
        service.withdraw_order(book(), form.to_dict(flat=True))
        return _redirect_back(form, '已撤回该处派查，别处不受影响')
    except DeskError as exc:
        return _redirect_back(form, str(exc), kind='err')


@bp.post('/desk/orders/report')
def ui_report():
    form = request.form
    try:
        result = service.submit_found(book(), form.to_dict(flat=True))
        if result['closed']:
            return _redirect_back(form, '双核实通过，找到回报已结单（整张工单仅结一次）')
        if result['order']['closed']:
            return _redirect_back(form, '工单已结单，本次回报仅留档')
        if not result['qualified']:
            return _redirect_back(form, '回报缺少行李牌或实物特征核实，暂不能结单', kind='err')
        return _redirect_back(form, '回报已记录')
    except DeskError as exc:
        return _redirect_back(form, str(exc), kind='err')


def page_context(run_ref=None, sample_name='dirty', emsg=None, notice=None):
    """Context consumed by the server-rendered 工单 page."""
    if run_ref is None:
        run_ref = f'sample:{sample_name or "dirty"}'
    try:
        resolved_ref, stages = resolve_source({'run': run_ref})
    except DeskError:
        resolved_ref, stages = f'sample:{sample_name or "dirty"}', compute(sample(sample_name or 'dirty'))
        run_ref = resolved_ref
    # Opening the board provisions the lost-contact orders idempotently and
    # retracts any active dispatch whose replay load was overturned.
    service.generate_orders(book(), resolved_ref, stages)
    service.reconcile(book(), resolved_ref, stages)
    view = service.board_for_run(book(), resolved_ref, stages)
    return dict(order_view=view, run_ref=resolved_ref, emsg=emsg, notice=notice,
                order_labels=ORDER_LABELS, version=VERSION)


ORDER_LABELS = {
    'open': '进行中', 'closed': '已结单',
    'dispatched': '已派查', 'withdrawn': '已撤回',
    'LOADED': '确认装机', 'OFFLOADED': '末态撤下', 'CONFLICT': '装撤冲突',
    'UNSEEN': '未见装机', 'PENDING': '待核',
    'LEG_UNVERIFIED': '航段归属待核', 'STATE_CONFLICT': '装撤同刻冲突',
    'NOT_LOADED': '回放未确认装机', 'FLIGHT_NOT_ARRIVED': '航班尚未到站',
    'ARRIVAL_UNKNOWN': '缺到站时刻', 'ALREADY_DISPATCHED': '已在查',
    'ORDER_CLOSED': '工单已结单',
}
