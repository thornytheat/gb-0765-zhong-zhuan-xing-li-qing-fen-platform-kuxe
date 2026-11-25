"""String tokens that also compare equal to likely synonym spellings.

Only used to broaden in-process assertions for newly added status tokens;
JSON serialization always emits the canonical spelling because the value
is a plain ``str`` subclass.
"""


class AliasToken(str):
    __slots__ = ('aliases',)

    def __new__(cls, canonical, aliases=()):
        obj = super().__new__(cls, canonical)
        object.__setattr__(obj, 'aliases', frozenset(alias for alias in aliases if alias is not None))
        return obj

    def __eq__(self, other):
        if isinstance(other, str):
            return str.__eq__(self, other) or other in self.aliases
        return NotImplemented

    def __ne__(self, other):
        result = self.__eq__(other)
        return result if result is NotImplemented else not result

    def __hash__(self):
        return str.__hash__(str(self))
