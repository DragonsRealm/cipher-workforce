"""Edge-condition equality must tolerate the editor's string targets.

The edge-condition editor (``client/src/types/EdgeCondition.ts``) types the
``eq``/``neq`` target as ``valueType: 'any'`` and stores whatever the user
typed, so a numeric comparison arrives as the string ``"200"`` while the node
output holds the integer ``200``. Strict ``==`` made that permanently false,
and a never-matching condition is indistinguishable from a mis-typed field
name -- the branch simply never fires and nothing is logged as wrong.

The ordering operators already coerced through ``_safe_compare``; these tests
lock equality to the same behaviour, and lock the boolean carve-out that keeps
``float(True) == 1.0`` from making ``eq 1`` match a ``True`` output.
"""

from __future__ import annotations

import pytest

from services.execution.conditions import evaluate_condition


def _cond(operator: str, value, field: str = "result.status_code") -> dict:
    return {"field": field, "operator": operator, "value": value}


class TestStringTargetAgainstNumericOutput:
    """The reported trap: `status_code eq 200` typed into the editor."""

    def test_eq_matches_string_target_against_int_output(self):
        output = {"result": {"status_code": 200}}
        assert evaluate_condition(_cond("eq", "200"), output) is True

    def test_eq_matches_int_target_against_string_output(self):
        output = {"result": {"status_code": "200"}}
        assert evaluate_condition(_cond("eq", 200), output) is True

    def test_eq_matches_float_and_int_forms(self):
        output = {"result": {"status_code": 200}}
        assert evaluate_condition(_cond("eq", "200.0"), output) is True

    def test_neq_is_the_exact_negation(self):
        output = {"result": {"status_code": 200}}
        assert evaluate_condition(_cond("neq", "200"), output) is False
        assert evaluate_condition(_cond("neq", "404"), output) is True

    def test_eq_still_rejects_a_genuine_mismatch(self):
        output = {"result": {"status_code": 200}}
        assert evaluate_condition(_cond("eq", "404"), output) is False


class TestNonNumericEqualityUnchanged:
    def test_plain_string_equality(self):
        output = {"result": {"status": "success"}}
        assert evaluate_condition(_cond("eq", "success", "result.status"), output) is True
        assert evaluate_condition(_cond("eq", "failure", "result.status"), output) is False

    def test_missing_field_does_not_match(self):
        assert evaluate_condition(_cond("eq", "200"), {"result": {}}) is False

    def test_none_target_only_matches_none(self):
        assert evaluate_condition(_cond("eq", None), {"result": {}}) is True
        assert evaluate_condition(_cond("eq", None), {"result": {"status_code": 200}}) is False


class TestBooleanCarveOut:
    """``float(True)`` is ``1.0``, so the new numeric coercion would have made
    a truthy flag satisfy ``eq "1"``. The guard blocks that path only.

    It deliberately does NOT touch ``True == 1``: that is plain Python
    equality, it short-circuits before the guard, and it matched under the
    old strict ``==`` too. Tightening it here would be a behaviour change
    smuggled in under a bug fix.
    """

    @pytest.mark.parametrize("target", ["1", "true", "True"])
    def test_true_does_not_equal_a_string(self, target):
        output = {"result": {"ok": True}}
        assert evaluate_condition(_cond("eq", target, "result.ok"), output) is False

    @pytest.mark.parametrize("target", ["0", "false", "False"])
    def test_false_does_not_equal_a_string(self, target):
        output = {"result": {"ok": False}}
        assert evaluate_condition(_cond("eq", target, "result.ok"), output) is False

    @pytest.mark.parametrize("target", [1, 1.0])
    def test_python_numeric_equality_is_preserved(self, target):
        """Pre-existing behaviour, asserted so a future tightening is a
        conscious decision rather than an accident."""
        output = {"result": {"ok": True}}
        assert evaluate_condition(_cond("eq", target, "result.ok"), output) is True

    def test_boolean_still_equals_itself(self):
        output = {"result": {"ok": True}}
        assert evaluate_condition(_cond("eq", True, "result.ok"), output) is True
        assert evaluate_condition(_cond("neq", False, "result.ok"), output) is True


class TestOrderingOperatorsUnaffected:
    """Guard against the equality change altering the comparison family."""

    def test_gt_lt_still_coerce(self):
        output = {"result": {"status_code": 200}}
        assert evaluate_condition(_cond("gt", "199"), output) is True
        assert evaluate_condition(_cond("lt", "201"), output) is True
        assert evaluate_condition(_cond("gte", "200"), output) is True
        assert evaluate_condition(_cond("lte", "200"), output) is True
