"""Discord-native presentation layer for Guinness Family.

The package intentionally contains no business logic.  Cogs provide data and
callbacks, while this layer turns them into Discord Components v2 payloads.
"""

from .layouts import ModernLayoutView, build_layout
from .design import (
    Accent,
    LAYOUT_SEQUENCE,
    LayoutLimitError,
    LayoutSlot,
    Status,
    card,
    compose,
    debug_validate_layout,
    divider,
    empty_state,
    heading,
    key_values,
    progress,
    section_with_action,
    section_with_thumbnail,
    stat_tile,
    status_badge,
    text_budget,
    validate_layout,
)
from .messages import (
    edit_interaction_message_v2,
    edit_message_v2,
    edit_original_response_v2,
    followup_v2,
    respond_v2,
    send_v2,
)
from .navigation import (
    ConfirmDialog,
    FilterBar,
    FilterSpec,
    PageState,
    Paginator,
    Tab,
    TabbedPanel,
    register_navigation,
)

__all__ = (
    "Accent",
    "ConfirmDialog",
    "FilterBar",
    "FilterSpec",
    "LAYOUT_SEQUENCE",
    "LayoutLimitError",
    "LayoutSlot",
    "ModernLayoutView",
    "PageState",
    "Paginator",
    "Status",
    "Tab",
    "TabbedPanel",
    "build_layout",
    "card",
    "compose",
    "debug_validate_layout",
    "divider",
    "edit_interaction_message_v2",
    "edit_message_v2",
    "edit_original_response_v2",
    "empty_state",
    "followup_v2",
    "heading",
    "key_values",
    "progress",
    "register_navigation",
    "respond_v2",
    "section_with_action",
    "section_with_thumbnail",
    "send_v2",
    "stat_tile",
    "status_badge",
    "text_budget",
    "validate_layout",
)
