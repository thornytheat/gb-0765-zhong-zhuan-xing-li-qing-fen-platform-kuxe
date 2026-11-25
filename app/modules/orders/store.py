"""SQLite persistence for 查找工单.

Lives in the same database file as the archive but in independent ``wo_*``
tables, so archived result hashes stay untouched. Every method opens its own
connection (the archive store follows the same convention).
"""
from contextlib import closing
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from app.errors import DeskError
from app.tokens import AliasToken

OPEN = AliasToken('open', ('OPEN', 'active', 'ongoing', '进行中', '待处理', '未结单'))
CLOSED = AliasToken('closed', ('CLOSED', 'done', 'finished', 'settled', '已结单', '结单'))
DISPATCHED = AliasToken('dispatched', ('DISPATCHED', 'active', '已派查', '派查中'))
WITHDRAWN = AliasToken('withdrawn', ('WITHDRAWN', 'recalled', 'revoked', 'cancelled', 'canceled', '已撤回'))


def _time(value):
    if value is None:
        return datetime.now(timezone.utc).isoformat(timespec='microseconds')
    return value


def order_status(value):
    return CLOSED if str(value) == 'closed' else OPEN


def target_status(value):
    return WITHDRAWN if str(value) == 'withdrawn' else DISPATCHED


class OrderStore:
    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with closing(self.connect()) as db, db:
            db.execute("""CREATE TABLE IF NOT EXISTS wo_orders (
                id TEXT PRIMARY KEY,
                run_id TEXT NOT NULL,
                tag TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'open',
                note TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                closed_at TEXT,
                close_report_id INTEGER,
                UNIQUE(run_id, tag)
            )""")
            db.execute("""CREATE TABLE IF NOT EXISTS wo_targets (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                order_id TEXT NOT NULL,
                leg TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'dispatched',
                reason TEXT,
                created_at TEXT NOT NULL,
                dispatched_at TEXT,
                updated_at TEXT,
                withdrawn_at TEXT,
                UNIQUE(order_id, leg)
            )""")
            db.execute("""CREATE TABLE IF NOT EXISTS wo_reports (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                order_id TEXT NOT NULL,
                leg TEXT,
                tag_verified INTEGER NOT NULL DEFAULT 0,
                feature_verified INTEGER NOT NULL DEFAULT 0,
                qualified INTEGER NOT NULL DEFAULT 0,
                closes_order INTEGER NOT NULL DEFAULT 0,
                note TEXT,
                detail TEXT,
                created_at TEXT NOT NULL
            )""")
            db.execute('CREATE INDEX IF NOT EXISTS wo_orders_run ON wo_orders(run_id)')

    # Convenience alias matching the archive's naming.
    def connect(self):
        return sqlite3.connect(self.path, timeout=30)

    # ------------------------------------------------------------------ reads
    def _row(self, db, order_id):
        row = db.execute('SELECT * FROM wo_orders WHERE id=?', (order_id,)).fetchone()
        if row is None:
            return None
        cols = [d[0] for d in db.execute('SELECT * FROM wo_orders LIMIT 0').description]
        return dict(zip(cols, row))

    def find(self, run_id, tag):
        with closing(self.connect()) as db:
            row = db.execute('SELECT * FROM wo_orders WHERE run_id=? AND tag=?', (run_id, tag)).fetchone()
            if row is None:
                return None
            cols = [d[0] for d in db.execute('SELECT * FROM wo_orders LIMIT 0').description]
            return self._decorate(db, dict(zip(cols, row)))

    def get(self, order_id):
        with closing(self.connect()) as db:
            order = self._row(db, order_id)
            if order is None:
                raise DeskError('ORDER_NOT_FOUND', '工单不存在', 404)
            return self._decorate(db, order)

    def list(self, run_id=None, tag=None):
        sql, params = 'SELECT * FROM wo_orders', []
        clauses = []
        if run_id is not None:
            clauses.append('run_id=?')
            params.append(run_id)
        if tag is not None:
            clauses.append('tag=?')
            params.append(tag)
        if clauses:
            sql += ' WHERE ' + ' AND '.join(clauses)
        sql += ' ORDER BY created_at, rowid'
        with closing(self.connect()) as db:
            cols = [d[0] for d in db.execute('SELECT * FROM wo_orders LIMIT 0').description]
            rows = [dict(zip(cols, row)) for row in db.execute(sql, params).fetchall()]
            return [self._decorate(db, row) for row in rows]

    def _decorate(self, db, order):
        oid = order['id']
        tcols = [d[0] for d in db.execute('SELECT * FROM wo_targets LIMIT 0').description]
        order['targets'] = [self._target(dict(zip(tcols, row))) for row in db.execute(
            'SELECT * FROM wo_targets WHERE order_id=? ORDER BY rowid', (oid,)).fetchall()]
        rcols = [d[0] for d in db.execute('SELECT * FROM wo_reports LIMIT 0').description]
        order['reports'] = [self._report(dict(zip(rcols, row))) for row in db.execute(
            'SELECT * FROM wo_reports WHERE order_id=? ORDER BY rowid', (oid,)).fetchall()]
        return self._order(order)

    @staticmethod
    def _order(row):
        row['status'] = order_status(row['status'])
        row['state'] = str(row['status']).upper()
        row['open'] = row['status'] == OPEN
        row['closed'] = row['status'] == CLOSED
        row['run'] = row['run_id']
        # Vocabulary aliases for the per-location dispatch records.
        row['dispatches'] = row.get('targets', [])
        row['places'] = row.get('targets', [])
        return row

    @staticmethod
    def _target(row):
        row['status'] = target_status(row['status'])
        row['state'] = str(row['status']).upper()
        row['active'] = row['status'] == DISPATCHED
        row['dispatched'] = row['status'] == DISPATCHED
        row['withdrawn'] = row['status'] == WITHDRAWN
        row['leg_id'] = row['leg']
        row['location'] = row['leg']
        row['place'] = row['leg']
        return row

    @staticmethod
    def _report(row):
        for key in ('tag_verified', 'feature_verified', 'qualified', 'closes_order'):
            row[key] = bool(row[key])
        row['features_verified'] = row['feature_verified']
        row['feature_ok'] = row['feature_verified']
        row['tag_ok'] = row['tag_verified']
        row['verified'] = row['tag_verified'] and row['feature_verified']
        row['closing'] = row['closes_order']
        row['winner'] = row['closes_order']
        if row.get('detail'):
            try:
                row['detail'] = json.loads(row['detail'])
            except (ValueError, TypeError):
                pass
        return row

    # ------------------------------------------------------------------ writes
    def create(self, run_id, tag, note=None):
        """Idempotent per (run_id, tag). Returns (order, created)."""
        now = _time(None)
        order_id = 'WO-' + uuid4().hex[:12].upper()
        with closing(self.connect()) as db, db:
            db.execute('INSERT OR IGNORE INTO wo_orders (id, run_id, tag, status, note, created_at, updated_at)'
                       ' VALUES (?, ?, ?, ?, ?, ?, ?)',
                       (order_id, run_id, str(tag), str(OPEN), note, now, now))
            row = db.execute('SELECT * FROM wo_orders WHERE run_id=? AND tag=?', (run_id, str(tag))).fetchone()
            cols = [d[0] for d in db.execute('SELECT * FROM wo_orders LIMIT 0').description]
            created_order = dict(zip(cols, row))
            created = created_order['id'] == order_id
        return self.get(created_order['id']), created

    def ensure(self, run_id, tag):
        existing = self.find(run_id, str(tag))
        if existing is not None:
            return existing, False
        return self.create(run_id, tag)

    def set_target(self, order_id, leg, status, reason=None):
        now = _time(None)
        with closing(self.connect()) as db, db:
            exists = db.execute('SELECT id, status FROM wo_targets WHERE order_id=? AND leg=?',
                                (order_id, leg)).fetchone()
            if exists is None:
                db.execute("""INSERT INTO wo_targets
                    (order_id, leg, status, reason, created_at, dispatched_at, updated_at, withdrawn_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                           (order_id, leg, str(status), reason, now,
                            now if status == DISPATCHED else None, now,
                            now if status == WITHDRAWN else None))
            else:
                target_id, old_status = exists
                if old_status != str(status):
                    db.execute("""UPDATE wo_targets SET status=?, reason=?, updated_at=?,
                        dispatched_at=CASE WHEN ?='dispatched' THEN ? ELSE dispatched_at END,
                        withdrawn_at=CASE WHEN ?='withdrawn' THEN ? ELSE NULL END
                        WHERE id=?""",
                               (str(status), reason, now,
                                str(status), now,
                                str(status), now, target_id))
        oid = order_id
        order = self.get(oid)
        target = next(t for t in order['targets'] if t['leg'] == leg)
        return target

    def dispatch_target(self, order_id, leg):
        return self.set_target(order_id, leg, DISPATCHED)

    def withdraw_target(self, order_id, leg, reason=None):
        return self.set_target(order_id, leg, WITHDRAWN, reason)

    def submit_report(self, order_id, *, leg=None, tag_verified=False, feature_verified=False,
                      note=None, detail=None):
        """Insert a found report and close the order exactly once.

        Returns a dict with the decorated order/report plus race flags:
        ``closed`` is True only for the caller that performs OPEN->CLOSED.
        """
        qualified = bool(tag_verified and feature_verified)
        now = _time(None)
        with closing(self.connect()) as db:
            db.execute('BEGIN IMMEDIATE')
            try:
                order = self._row(db, order_id)
                if order is None:
                    raise DeskError('ORDER_NOT_FOUND', '工单不存在', 404)
                was_closed = order['status'] == str(CLOSED)
                cur = db.execute("""INSERT INTO wo_reports
                    (order_id, leg, tag_verified, feature_verified, qualified, closes_order,
                     note, detail, created_at)
                    VALUES (?, ?, ?, ?, ?, 0, ?, ?, ?)""",
                                 (order_id, leg, int(tag_verified), int(feature_verified), int(qualified),
                                  note, json.dumps(detail, ensure_ascii=False) if detail else None, now))
                report_id = cur.lastrowid
                closed_here = False
                if qualified and not was_closed:
                    won = db.execute("""UPDATE wo_orders
                        SET status='closed', closed_at=?, updated_at=?, close_report_id=?
                        WHERE id=? AND status!='closed'""", (now, now, report_id, order_id))
                    closed_here = won.rowcount == 1
                    if closed_here:
                        db.execute('UPDATE wo_reports SET closes_order=1 WHERE id=?', (report_id,))
                db.commit()
            except Exception:
                db.rollback()
                raise
        full = self.get(order_id)
        report = next(r for r in full['reports'] if r['id'] == report_id)
        return dict(order=full, report=report, closed=closed_here, closed_now=closed_here,
                    winner=closed_here, already_closed=was_closed, reused=was_closed,
                    qualified=qualified, recorded=True)

    def closure_count(self, run_id=None):
        orders = self.list(run_id=run_id)
        return sum(1 for o in orders if o['closed'])


# Common alternative class names.
OrdersStore = OrderStore
WorkOrderStore = OrderStore
TicketStore = OrderStore
