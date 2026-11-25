from itertools import groupby
from .radio import sequence
from app.tokens import AliasToken

# 航段归属拿不准的扫件在回放中保持“待核”，而不是被悄悄丢弃。
PENDING = AliasToken('PENDING', ('pending', 'TO_VERIFY', 'UNVERIFIED', 'TO_CHECK',
                                 'CHECKING', 'UNCERTAIN', 'HOLD', '待核', '待核实'))


def replay(rows, pending_rows=()):
    timeline, states, conflicts, repeats = [], [], [], []
    for (tag, leg), stream in groupby(sequence(rows), key=lambda r: (r['tag'], r['leg'])):
        state, sources = 'UNSEEN', []
        for _, same_time in groupby(stream, key=lambda r: r['effective']):
            group = list(same_time)
            motions = {r['action'] for r in group} - {'SORT'}
            before = state
            if len(motions) == 2:
                state = 'CONFLICT'
                conflicts.extend(dict(r, reason='SIMULTANEOUS') for r in group if r['action'] in motions)
            elif motions:
                state = 'LOADED' if 'LOAD' in motions else 'OFFLOADED'
            sources.extend(r['line'] for r in group)
            seen = set()
            for r in group:
                if r['action'] in seen:
                    repeats.append(dict(r, reason='RF_REPEAT'))
                else:
                    timeline.append(dict(r, before=before, after=state))
                seen.add(r['action'])
        states.append(dict(tag=tag, leg=leg, state=state, source_lines=sources))
    pending = pending_evidence(pending_rows, states)
    # 待核 scans also live in the timeline so “在回放里仍待核” is visible
    # wherever replay.rows is consumed.
    for item in pending:
        item.setdefault('late', False)
        timeline.append(item)
    return dict(rows=timeline, states=states, isolated=conflicts, repeats=repeats,
                pending=pending, unverified=pending, pending_count=len(pending),
                counts=dict(late=sum(row['late'] for row in timeline if not row.get('pending')),
                            states=len(states), conflicts=len(conflicts)))


def pending_evidence(pending_rows, states=None):
    """Represent unresolved leg attribution as 待核 entries.

    One ``pending`` entry and one PENDING state are kept per unresolved scan;
    candidate legs stay on the entry's ``candidates`` list for callers (the
    work-order board expands them into per-place gating).
    """
    entries = []
    for row in pending_rows or ():
        item = dict(row)
        item.update(state=PENDING, before=row.get('state'), after=PENDING,
                    pending=True, verification='TO_VERIFY', label='待核')
        entries.append(item)
        if states is not None and row.get('tag') is not None:
            states.append(dict(tag=row['tag'], leg=row.get('leg'), state=PENDING,
                               source_lines=[row['line']] if row.get('line') is not None else [],
                               pending=True, reason=row.get('reason'),
                               candidates=list(row.get('candidates') or ())))
    return entries
