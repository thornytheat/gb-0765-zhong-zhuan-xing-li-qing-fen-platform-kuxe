from flask import Blueprint, jsonify, render_template, request
from .errors import DeskError
from .input import sample, submission
from .modules.archive.routes import store
from .modules.evidence.service import index
from .pipeline import compute
from .reasons import REASONS

DESK = {
    'scans': '扫描原件', 'legs': '航段候选', 'replay': '扫件回放', 'exceptions': '错载与漏扫',
    'ledger': '去向对账', 'archive': '批次留档', 'evidence': '联程证据', 'orders': '查找工单',
}
bp = Blueprint('desk', __name__)


def snapshot():
    run = request.args.get('run')
    name = request.args.get('sample', 'dirty')
    if run and str(run).startswith('sample:'):
        name = str(run).split(':', 1)[1]
        files = sample(name)
        return files, compute(files), None, name
    if run:
        files, result = store().get(run)
        return files, result['stages'], result['manifest'], name
    files = sample(name)
    return files, compute(files), None, name


@bp.get('/api/stage/<key>')
def stage_api(key):
    if key == 'orders':
        from .modules.orders.routes import board_api
        return board_api()
    if key not in tuple(DESK)[:5]:
        raise DeskError('NO_STAGE', '业务入口不存在', 404)
    run = request.args.get('run')
    if run:
        return jsonify(store().get(run)[1]['stages'][key])
    return jsonify(compute(sample(request.args.get('sample', 'dirty')), until=key))


@bp.post('/api/stage/<key>')
def inspect_stage(key):
    if key == 'orders':
        from .modules.orders.routes import create_or_act
        return create_or_act()
    if key not in tuple(DESK)[:5]:
        raise DeskError('NO_STAGE', '业务入口不存在', 404)
    return jsonify(compute(submission(request.get_json(silent=True)), until=key))


@bp.get('/', defaults={'key': 'ledger'})
@bp.get('/desk/<key>')
def desk(key):
    if key not in DESK:
        raise DeskError('NO_STAGE', '业务入口不存在', 404)
    files, stages, manifest, name = snapshot()
    context = dict(stages=stages, ledger=stages['ledger']['counts'], names=DESK, key=key, reasons=REASONS,
                   selected=name, manifest=manifest, archive=store().list() if key == 'archive' else [],
                   report=index(files, dict(stages=stages, manifest=manifest)) if key == 'evidence' and manifest else None)
    if key == 'orders':
        from .modules.orders.routes import page_context
        run_ref = manifest['id'] if manifest else f'sample:{name}'
        context.update(page_context(run_ref=request.args.get('run') or run_ref, sample_name=name,
                                    emsg=request.args.get('emsg'), notice=request.args.get('notice')))
    return render_template('desk.html', **context)


@bp.get('/desk/work-orders')
@bp.get('/desk/work_orders')
@bp.get('/desk/order')
@bp.get('/desk/tickets')
@bp.get('/desk/dispatch')
@bp.get('/desk/tracing')
@bp.get('/desk/trace')
@bp.get('/desk/search')
@bp.get('/orders')
@bp.get('/work-orders')
def desk_orders_alias():
    return desk('orders')
