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


class TestTextToTextIsNeverCoerced:
    """The coercion bridges text to a number, never text to text.

    A first cut coerced both sides whenever neither was bool/None. That looked
    harmless and was not: ``float()`` rounds past 2**53, so two *distinct*
    18-digit identifiers compared equal and silently took each other's branch.
    WhatsApp group JIDs, snowflake ids and nanosecond timestamps all live in
    that range, and `eq` on an id is one of the most natural conditions to
    write -- so the failure would have been both common and invisible.
    """

    @pytest.mark.parametrize(
        "left,right",
        [
            ("120363012345678901", "120363012345678902"),  # WhatsApp group JIDs
            ("1234567890123456789", "1234567890123456788"),  # snowflake ids
            ("1700000000000000001", "1700000000000000002"),  # ns timestamps
        ],
    )
    def test_distinct_long_numeric_strings_are_not_equal(self, left, right):
        output = {"result": {"chat_id": left}}
        assert evaluate_condition(_cond("eq", right, "result.chat_id"), output) is False

    def test_identical_long_numeric_string_still_matches(self):
        jid = "120363012345678901"
        output = {"result": {"chat_id": jid}}
        assert evaluate_condition(_cond("eq", jid, "result.chat_id"), output) is True

    def test_precision_holds_when_one_side_is_a_real_int(self):
        """Exactness comes from Decimal, not from refusing to compare."""
        output = {"result": {"chat_id": 120363012345678901}}
        assert evaluate_condition(_cond("eq", "120363012345678902", "result.chat_id"), output) is False
        assert evaluate_condition(_cond("eq", "120363012345678901", "result.chat_id"), output) is True

    def test_unrelated_strings_compare_as_strings(self):
        output = {"result": {"status": "success"}}
        assert evaluate_condition(_cond("eq", "failure", "result.status"), output) is False


class TestNoOverflowOnHugeIntegers:
    """``float(10**400)`` raises OverflowError, which is not ValueError or
    TypeError -- it escaped the helper, so ``neq`` answered False for a pair
    that was plainly unequal. Decimal takes arbitrary-precision ints."""

    def test_huge_int_does_not_raise_and_compares_correctly(self):
        output = {"result": {"n": 10**400}}
        assert evaluate_condition(_cond("neq", "1", "result.n"), output) is True
        assert evaluate_condition(_cond("eq", "1", "result.n"), output) is False

    def test_huge_int_equal_to_its_own_text(self):
        output = {"result": {"n": 10**400}}
        assert evaluate_condition(_cond("eq", str(10**400), "result.n"), output) is True


class TestBooleanCarveOut:
    """No boolean special case exists: ``Decimal("True")`` is invalid, so a
    truthy flag cannot reach a number through the bridge.

    ``True == 1`` deliberately still matches -- that is plain Python equality,
    it short-circuits first, and it matched under the old strict ``==`` too.
    Tightening it would be a behaviour change smuggled in under a bug fix.
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
