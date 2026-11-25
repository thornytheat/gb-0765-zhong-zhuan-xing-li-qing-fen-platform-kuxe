"""Import-path alias: trace."""
import sys

from app.modules.orders import service, store, routes  # noqa: F401
from app.modules.orders.service import *  # noqa: F401,F403
from app.modules.orders.store import OrderStore  # noqa: F401

sys.modules[__name__ + '.service'] = service
sys.modules[__name__ + '.store'] = store
sys.modules[__name__ + '.routes'] = routes
