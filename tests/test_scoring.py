"""
tests/test_scoring.py — Unit tests for the two-axis scoring model.

Run with: pytest tests/test_scoring.py -v
"""

from datetime import date, timedelta

from scoring.score import (
    assign_tier,
    calc_deal_score,
    calc_distress_score,
    score_lead,
)


# =============================================================================
# Helpers
# =============================================================================

def days_ago(n: int) -> str:
    return (date.today() - timedelta(days=n)).isoformat()


def years_ago(n: float) -> str:
    return (date.today() - timedelta(days=int(n * 365.25))).isoformat()


# =============================================================================
# Axis 1 — Distress Score
# =============================================================================

class TestDistressScore:

    def test_probate_base(self):
        lead = {"source_type": "probate"}
        assert calc_distress_score(lead) == 15

    def test_foreclosure_base(self):
        lead = {"source_type": "foreclosure"}
        assert calc_distress_score(lead) == 10

    def test_code_violation_base(self):
        lead = {"source_type": "code_violation"}
        assert calc_distress_score(lead) == 10

    def test_tax_lien_base(self):
        lead = {"source_type": "tax_lien"}
        assert calc_distress_score(lead) == 8

    def test_vacant_flag_adds_5(self):
        lead = {"source_type": "probate", "vacant_flag": True}
        assert calc_distress_score(lead) == 20

    def test_out_of_state_adds_5(self):
        lead = {"source_type": "probate", "owner_out_of_state": True}
        assert calc_distress_score(lead) == 20

    def test_freshness_bonus_within_30_days(self):
        lead = {"source_type": "probate", "filing_date": days_ago(10)}
        assert calc_distress_score(lead) == 18  # 15 + 3

    def test_no_freshness_bonus_older_than_30_days(self):
        lead = {"source_type": "probate", "filing_date": days_ago(45)}
        assert calc_distress_score(lead) == 15

    def test_stacked_primary_signals_bonus(self):
        # probate + tax_lien stacked
        lead = {
            "source_type": "probate",
            "stacked_sources": ["probate", "tax_lien"],
        }
        assert calc_distress_score(lead) == 20  # 15 + 5 stacked bonus

    def test_tier_d_divorce_bonus_with_primary(self):
        lead = {
            "source_type": "probate",
            "stacked_sources": ["divorce"],
        }
        assert calc_distress_score(lead) == 19  # 15 + 4

    def test_tier_d_all_three_bonuses_capped_at_12(self):
        lead = {
            "source_type": "probate",
            "stacked_sources": ["divorce", "eviction", "bankruptcy"],
        }
        # 15 + min(4+4+4, 12) = 15 + 12 = 27
        assert calc_distress_score(lead) == 27

    def test_tier_d_alone_no_bonus(self):
        # Tier D signal with no primary — no Tier D bonus applied
        lead = {"source_type": "divorce", "stacked_sources": ["eviction"]}
        assert calc_distress_score(lead) == 0

    def test_max_score_capped_at_50(self):
        lead = {
            "source_type": "probate",
            "vacant_flag": True,
            "owner_out_of_state": True,
            "filing_date": days_ago(5),
            "stacked_sources": ["probate", "tax_lien", "divorce", "eviction", "bankruptcy"],
        }
        result = calc_distress_score(lead)
        assert result <= 50

    def test_unknown_source_type_scores_zero(self):
        lead = {"source_type": "referral"}
        assert calc_distress_score(lead) == 0


# =============================================================================
# Axis 2 — Deal Score
# =============================================================================

class TestDealScore:

    def test_equity_over_50_pct(self):
        lead = {"estimated_equity_pct": 65.0}
        assert calc_deal_score(lead) == 25

    def test_equity_30_to_50_pct(self):
        lead = {"estimated_equity_pct": 40.0}
        assert calc_deal_score(lead) == 20

    def test_equity_15_to_30_pct(self):
        lead = {"estimated_equity_pct": 22.0}
        assert calc_deal_score(lead) == 12

    def test_equity_under_15_pct(self):
        lead = {"estimated_equity_pct": 8.0}
        assert calc_deal_score(lead) == 5

    def test_equity_unknown_flag(self):
        lead = {"equity_unknown": True}
        assert calc_deal_score(lead) == 8

    def test_equity_none_treated_as_unknown(self):
        lead = {"estimated_equity_pct": None}
        assert calc_deal_score(lead) == 8

    def test_last_sale_over_10_years(self):
        lead = {"estimated_equity_pct": 60.0, "last_sale_date": years_ago(12)}
        assert calc_deal_score(lead) == 35  # 25 + 10

    def test_last_sale_5_to_10_years(self):
        lead = {"estimated_equity_pct": 60.0, "last_sale_date": years_ago(7)}
        assert calc_deal_score(lead) == 31  # 25 + 6

    def test_last_sale_under_5_years_no_bonus(self):
        lead = {"estimated_equity_pct": 60.0, "last_sale_date": years_ago(3)}
        assert calc_deal_score(lead) == 25  # 25 + 0

    def test_ohio_sweet_spot_value(self):
        lead = {"estimated_equity_pct": 60.0, "estimated_value": 150_000}
        assert calc_deal_score(lead) == 35  # 25 + 10

    def test_value_300k_to_500k(self):
        lead = {"estimated_equity_pct": 60.0, "estimated_value": 400_000}
        assert calc_deal_score(lead) == 30  # 25 + 5

    def test_value_under_75k_no_bonus(self):
        lead = {"estimated_equity_pct": 60.0, "estimated_value": 50_000}
        assert calc_deal_score(lead) == 25  # 25 + 0

    def test_value_over_500k_no_bonus(self):
        lead = {"estimated_equity_pct": 60.0, "estimated_value": 600_000}
        assert calc_deal_score(lead) == 25  # 25 + 0

    def test_max_score_capped_at_50(self):
        lead = {
            "estimated_equity_pct": 75.0,
            "last_sale_date": years_ago(15),
            "estimated_value": 200_000,
        }
        result = calc_deal_score(lead)
        assert result <= 50

    def test_all_missing_fields_scores_8(self):
        lead = {}
        assert calc_deal_score(lead) == 8  # equity unknown neutral


# =============================================================================
# Combined scoring and tier assignment
# =============================================================================

class TestAssignTier:

    def test_tier_a_at_40(self):
        assert assign_tier(40) == "A"

    def test_tier_a_at_100(self):
        assert assign_tier(100) == "A"

    def test_tier_b_at_35(self):
        assert assign_tier(35) == "B"

    def test_tier_b_at_39(self):
        assert assign_tier(39) == "B"

    def test_tier_c_at_20(self):
        assert assign_tier(20) == "C"

    def test_tier_c_at_34(self):
        assert assign_tier(34) == "C"

    def test_tier_d_at_0(self):
        assert assign_tier(0) == "D"

    def test_tier_d_at_19(self):
        assert assign_tier(19) == "D"


class TestScoreLead:

    def test_high_value_probate_tier_a(self):
        # probate(15) + out_of_state(5) + freshness(3) + stacked_tax_lien(5) = 28 distress
        # equity_60(25) + last_sale_12yr(10) + value_180k(10) = 45 deal
        # total = 73 → Tier A
        lead = {
            "source_type": "probate",
            "filing_date": days_ago(10),
            "owner_out_of_state": True,
            "stacked_sources": ["probate", "tax_lien"],
            "estimated_equity_pct": 60.0,
            "last_sale_date": years_ago(12),
            "estimated_value": 180_000,
        }
        result = score_lead(lead)
        assert result["tier"] == "A"
        assert result["score"] >= 70
        assert result["distress_score"] + result["deal_score"] == result["score"]

    def test_tier_d_divorce_alone_never_routes(self):
        lead = {
            "source_type": "divorce",
            "estimated_equity_pct": 70.0,
            "last_sale_date": years_ago(15),
            "estimated_value": 200_000,
        }
        result = score_lead(lead)
        # Distress score = 0 (divorce alone, no primary signal), deal score high
        # Tier D sources are capped at Tier C even with strong deal economics
        assert result["distress_score"] == 0
        assert result["tier"] in ("C", "D")

    def test_no_data_scores_low(self):
        lead = {"source_type": "tax_lien"}
        result = score_lead(lead)
        assert result["score"] < 45
        assert result["tier"] in ("C", "D")

    def test_result_keys_present(self):
        lead = {"source_type": "probate"}
        result = score_lead(lead)
        assert "distress_score" in result
        assert "deal_score" in result
        assert "score" in result
        assert "tier" in result

    def test_scores_sum_correctly(self):
        lead = {
            "source_type": "foreclosure",
            "estimated_equity_pct": 35.0,
            "estimated_value": 120_000,
        }
        result = score_lead(lead)
        assert result["score"] == result["distress_score"] + result["deal_score"]
