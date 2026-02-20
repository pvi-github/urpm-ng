"""Resolution module mixins for Resolver.

Each mixin provides a group of related resolution operations:
- PoolMixin: Pool creation and loading
- QueriesMixin: Capability and dependency queries
- AlternativesMixin: Alternative selection logic
- OrphansMixin: Orphan package detection
"""

from .pool import PoolMixin
from .queries import QueriesMixin
from .alternatives import AlternativesMixin
from .orphans import OrphansMixin

__all__ = [
    'PoolMixin',
    'QueriesMixin',
    'AlternativesMixin',
    'OrphansMixin',
]
