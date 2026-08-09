"""Unit tests for ports_gfx.actions — the semantic action vocabulary."""

from __future__ import annotations

from ports_gfx.actions import ACTION_DIRECTIONS, DIRECTION_ACTIONS, DIRECTIONAL_ACTIONS, Action


class TestActionDirections:
    def test_every_directional_action_has_a_vector(self):
        for action in DIRECTIONAL_ACTIONS:
            assert action in ACTION_DIRECTIONS

    def test_vectors_are_unit_and_distinct(self):
        vectors = list(ACTION_DIRECTIONS.values())
        assert len(set(vectors)) == len(vectors)
        for dx, dy in vectors:
            assert abs(dx) + abs(dy) == 1

    def test_direction_actions_is_the_exact_inverse(self):
        for action, vector in ACTION_DIRECTIONS.items():
            assert DIRECTION_ACTIONS[vector] is action

    def test_non_directional_actions_excluded(self):
        for action in (Action.CONFIRM, Action.BACK, Action.MENU, Action.TEXT_INPUT):
            assert action not in ACTION_DIRECTIONS
