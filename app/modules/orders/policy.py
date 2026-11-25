"""Pure 查找工单 policy predicates (no IO / Flask)."""
from .service import (  # noqa: F401
    LOST_STATUSES,
    build_board,
    can_dispatch,
    evaluate_place,
    feature_verified,
    moment,
    tag_verified,
    tri_state,
    truthy,
)

DISPATCHABLE_STATES = ('LOADED',)
PENDING_STATES = ('PENDING', 'TO_VERIFY', 'UNVERIFIED')


def place_is_dispatchable(state, arrived):
    """Rule: replay confirms loading AND flight has reached the station."""
    return str(state) in DISPATCHABLE_STATES and truthy(arrived)


def report_closes_order(tag_ok, feature_ok):
    """Rule: both baggage-tag and physical-feature checks are required."""
    return truthy(tag_ok) and truthy(feature_ok)
