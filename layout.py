"""Row layout: choose photos to fill a canvas side by side, never cropped.

A row places photos left to right at a common height. Given the shapes
(aspect ratios) of the candidate photos and the size of the canvas, plan_row()
picks which photos to use and where they go so that as much of the canvas as
possible is covered. Nothing is cropped: photos that don't fill the canvas
leave black margins instead.

This generalises the original "pair two portraits" behaviour, and is what
lets a single wide canvas (for instance several monitors spanned into one
desktop) be filled with as many photos as fit. The module is pure geometry --
no image handling -- so it can be reasoned about and tested with plain
numbers.
"""
from __future__ import annotations

import itertools
from collections.abc import Callable, Sequence
from dataclasses import dataclass

# Pixels between neighbouring photos in a row.
DEFAULT_GAP_PX = 6
# A photo is never shrunk below this fraction of the canvas height to make
# room for its neighbours: beyond that, fewer photos look better.
MIN_ROW_SCALE = 0.7
# Adding another photo must cover at least this much more of the canvas
# (as a fraction of its area), or the simpler layout wins.
IMPROVEMENT_MARGIN = 0.02
# Candidates beyond this many are ignored, which bounds the search.
MAX_CANDIDATES = 12


@dataclass(frozen=True)
class Placement:
    """Where one photo goes on the canvas.

    Attributes:
        index: Position of the photo in the candidate list given to
            plan_row().
        x: Left edge, in pixels.
        y: Top edge, in pixels.
        width: Width, in pixels.
        height: Height, in pixels.
    """

    index: int
    x: int
    y: int
    width: int
    height: int


def _scale_to_fit(
    aspects: Sequence[float], width: int, height: int, gap: int,
) -> float:
    """Scale (at most 1) at which the photos fit side by side in `width`.

    The photos share one height. 1.0 means full canvas height; smaller
    values mean the row had to shrink to fit the width.
    """
    room = width - gap * (len(aspects) - 1)
    natural_width = height * sum(aspects)
    return min(1.0, room / natural_width)


def _coverage(
    aspects: Sequence[float], scale: float, width: int, height: int,
) -> float:
    """Fraction of the canvas area covered by photos at this scale."""
    return scale * scale * height * sum(aspects) / width


def _place(
    indices: Sequence[int], aspects: Sequence[float], scale: float,
    width: int, height: int, gap: int,
) -> list[Placement]:
    """Turn a chosen combination into pixel rectangles, centred.

    A row that had to shrink to fit uses every pixel of the width (no
    rounding sliver at the edge); one that fits at full height is centred
    with margins either side.
    """
    chosen = [aspects[i] for i in indices]
    room = width - gap * (len(chosen) - 1)
    if scale < 1.0:
        row_height = max(1, int(room / sum(chosen)))
        widths = _split_exactly(room, chosen)
    else:
        row_height = height
        widths = [max(1, int(aspect * row_height)) for aspect in chosen]
    total = sum(widths) + gap * (len(widths) - 1)
    x = max(0, (width - total) // 2)
    y = max(0, (height - row_height) // 2)
    placements = []
    for index, photo_width in zip(indices, widths):
        placements.append(Placement(index, x, y, photo_width, row_height))
        x += photo_width + gap
    return placements


def _split_exactly(total: int, weights: Sequence[float]) -> list[int]:
    """Split `total` pixels in proportion to `weights`, summing exactly.

    Each share is rounded down, then the leftover pixels go to the shares
    that lost the most to rounding.
    """
    exact = [total * weight / sum(weights) for weight in weights]
    shares = [max(1, int(value)) for value in exact]
    leftover = total - sum(shares)
    by_loss = sorted(range(len(shares)),
                     key=lambda i: exact[i] - shares[i], reverse=True)
    for i in by_loss[:max(0, leftover)]:
        shares[i] += 1
    return shares


def plan_row(
    aspects: Sequence[float], width: int, height: int, *,
    gap: int = DEFAULT_GAP_PX, max_photos: int = 2,
    companion_ok: Callable[[int], bool] | None = None,
) -> list[Placement]:
    """Choose photos from `aspects` and place them to fill the canvas.

    Args:
        aspects: Width/height ratio of each candidate photo, in order of
            preference. The first is the "seed" and is always used, which
            keeps the overall choice random when the caller shuffles.
        width: Canvas width in pixels.
        height: Canvas height in pixels.
        gap: Pixels between neighbouring photos.
        max_photos: Most photos allowed in the row.
        companion_ok: Optional filter on which candidates (by index) may
            join the seed, e.g. to restrict pairing to portraits.

    Returns:
        Placements for the chosen photos, left to right (empty if there
        are no candidates). Extra photos are added only when they cover
        noticeably more of the canvas without shrinking the row below
        MIN_ROW_SCALE.
    """
    if not aspects or width <= 0 or height <= 0:
        return []
    limit = min(len(aspects), MAX_CANDIDATES)
    companions = [
        index for index in range(1, limit)
        if companion_ok is None or companion_ok(index)
    ]

    seed_scale = _scale_to_fit(aspects[:1], width, height, gap)
    best_indices: tuple[int, ...] = (0,)
    best_scale = seed_scale
    best_coverage = _coverage(aspects[:1], seed_scale, width, height)

    for extra in range(1, max_photos):
        round_best = None
        for combo in itertools.combinations(companions, extra):
            indices = (0, *combo)
            chosen = [aspects[i] for i in indices]
            scale = _scale_to_fit(chosen, width, height, gap)
            if scale < MIN_ROW_SCALE:
                continue
            coverage = _coverage(chosen, scale, width, height)
            if round_best is None or coverage > round_best[0]:
                round_best = (coverage, indices, scale)
        # Only accept a bigger row if it is meaningfully better than the
        # best simpler one found so far.
        if round_best and round_best[0] > best_coverage + IMPROVEMENT_MARGIN:
            best_coverage, best_indices, best_scale = round_best

    return _place(best_indices, aspects, best_scale, width, height, gap)
