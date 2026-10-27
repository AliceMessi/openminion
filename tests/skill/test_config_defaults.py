"""Skill config default-value tests."""

from __future__ import annotations

from openminion.modules.skill.config import SkillConfig, load_config


def test_selection_rag_defaults_are_conservative() -> None:
    cfg = SkillConfig()
    assert cfg.selection_rag_threshold == 10
    assert cfg.selection_rag_topk == 5


def test_promotion_cadence_defaults_are_opt_in() -> None:
    cfg = SkillConfig()
    assert cfg.promotion_cadence_enabled is False
    assert cfg.promotion_cadence_success_threshold == 3
    assert cfg.promotion_cadence_utility_threshold == 0.7


def test_promotion_cadence_threshold_field_types_are_stable() -> None:
    cfg = SkillConfig()
    assert isinstance(cfg.promotion_cadence_success_threshold, int)
    assert isinstance(cfg.promotion_cadence_utility_threshold, float)


def test_load_config_parses_runtime_tunables() -> None:
    cfg = load_config(
        {
            "skill": {
                "selection_rag_threshold": 24,
                "selection_rag_topk": 7,
                "promotion_cadence_enabled": True,
                "promotion_cadence_success_threshold": 4,
                "promotion_cadence_utility_threshold": 0.8,
                "suggestion_batch_cap": 3,
                "suggestion_cooldown_seconds": 120,
                "suggestion_min_age_seconds": 30,
            }
        }
    )

    assert cfg.selection_rag_threshold == 24
    assert cfg.selection_rag_topk == 7
    assert cfg.promotion_cadence_enabled is True
    assert cfg.promotion_cadence_success_threshold == 4
    assert cfg.promotion_cadence_utility_threshold == 0.8
    assert cfg.suggestion_batch_cap == 3
    assert cfg.suggestion_cooldown_seconds == 120
    assert cfg.suggestion_min_age_seconds == 30
