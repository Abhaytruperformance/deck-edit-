"""Minimal in-memory stand-in for the bits of the Supabase client that
app/routers/editor.py and app/routers/publish.py call. Just enough chaining
to drive the real route logic in tests/test_versioning.py without a network.
"""
import uuid
from datetime import datetime, timezone

# Mirrors the `default ...` clauses in migrations/001_init.sql - real Postgres
# fills these in on insert (and returns them), so the fake must too or a test
# fixture that omits them (same as a minimal real insert would) diverges from
# production behavior for reasons that have nothing to do with the code under test.
_COLUMN_DEFAULTS = {
    "projects": {"status": "draft"},
    "artifacts": {"blocks": [], "current_version_id": None},
    "shares": {"view_count": 0, "password_hash": None, "expires_at": None},
}


class _Result:
    def __init__(self, data):
        self.data = data


class _Query:
    def __init__(self, store: list[dict], table: str):
        self._store = store
        self._table = table
        self._filters: list[tuple[str, object]] = []
        self._order_col = None
        self._order_desc = False
        self._limit = None
        self._insert_payload = None
        self._update_payload = None
        self._delete = False
        self._single = False

    def select(self, *_args):
        return self

    def eq(self, col, val):
        self._filters.append((col, val))
        return self

    def order(self, col, desc=False):
        self._order_col, self._order_desc = col, desc
        return self

    def limit(self, n):
        self._limit = n
        return self

    def single(self):
        self._single = True
        return self

    def insert(self, payload: dict):
        self._insert_payload = payload
        return self

    def update(self, payload: dict):
        self._update_payload = payload
        return self

    def delete(self):
        self._delete = True
        return self

    def _matches(self, row):
        return all(row.get(col) == val for col, val in self._filters)

    def execute(self):
        if self._insert_payload is not None:
            now = datetime.now(timezone.utc).isoformat()
            row = {**_COLUMN_DEFAULTS.get(self._table, {}), "created_at": now, "updated_at": now, **self._insert_payload}
            row.setdefault("id", str(uuid.uuid4()))
            self._store.append(row)
            return _Result([row])

        if self._update_payload is not None:
            updated = []
            for row in self._store:
                if self._matches(row):
                    row.update(self._update_payload)
                    updated.append(row)
            return _Result(updated)

        if self._delete:
            removed = [r for r in self._store if self._matches(r)]
            self._store[:] = [r for r in self._store if not self._matches(r)]
            return _Result(removed)

        rows = [r for r in self._store if self._matches(r)]
        if self._order_col:
            rows = sorted(rows, key=lambda r: r[self._order_col], reverse=self._order_desc)
        if self._limit is not None:
            rows = rows[: self._limit]

        if self._single:
            if len(rows) != 1:
                raise ValueError(f"single() expected exactly 1 row in {self._table}, got {len(rows)}")
            return _Result(rows[0])
        return _Result(rows)


class _FakeUser:
    def __init__(self, id: str):
        self.id = id


class _FakeAuthResult:
    def __init__(self, user_id: str):
        self.user = _FakeUser(user_id)


class _FakeAuth:
    """Just enough of supabase-py's .auth to drive app.auth.sign_up()/sign_in()
    (workspace creation/joining logic) without a real Supabase project -
    doesn't validate passwords, that's Supabase's own concern, not ours."""

    def __init__(self):
        self._users: dict[str, str] = {}  # email -> user id

    def sign_up(self, credentials: dict) -> _FakeAuthResult:
        email = credentials["email"]
        user_id = self._users.get(email) or str(uuid.uuid4())
        self._users[email] = user_id
        return _FakeAuthResult(user_id)

    def sign_in_with_password(self, credentials: dict) -> _FakeAuthResult:
        email = credentials["email"]
        if email not in self._users:
            raise ValueError("invalid credentials")
        return _FakeAuthResult(self._users[email])


class FakeSupabase:
    def __init__(self):
        self._tables: dict[str, list[dict]] = {}
        self.auth = _FakeAuth()

    def table(self, name: str) -> _Query:
        return _Query(self._tables.setdefault(name, []), name)
