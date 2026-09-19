"""One module per user role, because that is how `docs/08` §5 divides them.

The split is by *who is looking* rather than by what is being shown, and that
is deliberate: an owner and a lab user both need to see an experiment node, and
what separates their screens is not the data but what they may do with it. A
module per role is the shape that keeps "what may this person do" answerable by
reading one file.

Nothing here composes another screen. The three are alternatives, and which one
is mounted is `ravel.tui.app`'s decision — from the role the Gateway reported,
never from an argument.
"""

from __future__ import annotations

from ravel.tui.screens.admin import AdminScreen
from ravel.tui.screens.lab import LabScreen
from ravel.tui.screens.owner import OwnerScreen

__all__ = ["AdminScreen", "LabScreen", "OwnerScreen"]
