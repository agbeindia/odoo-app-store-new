import contextlib
from unittest.mock import Mock

import odoo.http


@contextlib.contextmanager
def mock_request(env, uid=None, sid='test-session-id', remote_addr='127.0.0.1'):
    """Put a minimal, *real* ``Session`` on the request stack.

    The session has to be a genuine ``odoo.http.Session`` and not a dict: the
    code under test relies on ``sid``, ``should_rotate`` and ``can_save``, and
    on the dirty-tracking that ``Session.__setitem__`` performs. Everything
    else about the request is mocked, because nothing else is touched.
    """
    session = odoo.http.Session(odoo.http.get_default_session(), sid)
    session.uid = uid if uid is not None else env.uid
    session.db = env.registry.db_name

    request = Mock(
        httprequest=Mock(remote_addr=remote_addr),
        session=session,
        env=env,
        db=env.registry.db_name,
        registry=env.registry,
        future_response=odoo.http.FutureResponse(),
    )

    odoo.http._request_stack.push(request)
    try:
        yield request
    finally:
        odoo.http._request_stack.pop()
