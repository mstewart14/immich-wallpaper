"""Pure helpers for multi-monitor wallpapers.

Two questions come up once there is more than one monitor, and neither
needs any image or desktop code:

* Which monitors should rotation touch? (select_monitors)
* If several monitors share one wide picture, where does each monitor's
  slice of it come from? (strip_layout)
"""
from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass

from desktops import Monitor


def select_monitors(
    monitors: Sequence[Monitor], wanted: Sequence[str] | None,
) -> list[Monitor]:
    """The monitors rotation should change, in their original order.

    An empty or missing `wanted` means every monitor. Otherwise only the
    monitors whose connector name is listed, and any listed name that isn't
    currently connected is skipped (it may simply be unplugged), so the
    result can be empty.
    """
    if not wanted:
        return list(monitors)
    names = set(wanted)
    return [monitor for monitor in monitors if monitor.name in names]


@dataclass(frozen=True)
class Slot:
    """Where one monitor's slice sits within a shared strip picture.

    Attributes:
        name: The monitor's connector name.
        x: Left edge of its slice, in strip pixels.
        y: Top edge of its slice, in strip pixels.
        width: Slice width (the monitor's width).
        height: Slice height (the monitor's height).
    """

    name: str
    x: int
    y: int
    width: int
    height: int


@dataclass(frozen=True)
class Strip:
    """Selected monitors laid side by side as one wide canvas.

    Attributes:
        width: Total width: the monitors' widths added up.
        height: Height of the tallest monitor.
        slots: Each monitor's slice, left to right.
    """

    width: int
    height: int
    slots: tuple[Slot, ...]


def strip_layout(monitors: Sequence[Monitor]) -> Strip:
    """Lay `monitors` side by side, in left-to-right order, as one strip.

    The strip is contiguous and ignores the monitors' real positions, so
    a monitor left out of the selection (say the middle one of three)
    leaves no gap that a photo could be cut across, and no picture is
    ever hidden behind a screen the user didn't choose. Monitors of
    different heights are centred vertically. Their real vertical offsets
    are not modelled.
    """
    ordered = sorted(monitors, key=lambda monitor: (monitor.x, monitor.y))
    if not ordered:
        return Strip(0, 0, ())
    height = max(monitor.height for monitor in ordered)
    slots = []
    x = 0
    for monitor in ordered:
        slots.append(Slot(monitor.name, x, (height - monitor.height) // 2,
                          monitor.width, monitor.height))
        x += monitor.width
    return Strip(x, height, tuple(slots))


def safe_name(name: str) -> str:
    """A monitor name made safe to use in a file name."""
    return re.sub(r"[^A-Za-z0-9_.-]", "_", name)
