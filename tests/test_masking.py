from __future__ import annotations

import pytest

from governed_metrics import masking
from governed_metrics.errors import Failure

PERSONAL = frozenset({"customer_email", "phone"})


def test_unknown_result_shapes_are_withheld():
    for result in ({"value": 1}, {"kind": "mystery"}, ["not", "a", "dict"]):
        with pytest.raises(Failure) as e:
            masking.apply(result, unmasked=True, personal_columns=PERSONAL)
        assert e.value.reason == "unrecognised_result_shape"


def test_email_shaped_values_are_masked_everywhere_but_the_callers_own_identity():
    out = masking.apply({"kind": "error", "detail": "no match for jane.doe@example.com",
                         "caller": "me@example.com"}, unmasked=False, personal_columns=PERSONAL)
    assert out["detail"] == "no match for j***@example.com"
    assert out["caller"] == "me@example.com"


def test_personal_breakdown_labels_are_masked():
    result = {"kind": "breakdown", "dimension_personal": True,
              "rows": [{"dimension": "a@example.com", "value": 3}, {"dimension": "07700900123", "value": 1}]}
    out = masking.apply(result, unmasked=False, personal_columns=PERSONAL)
    assert [r["dimension"] for r in out["rows"]] == ["a***@example.com", "0***"]
    assert out["rows"][0]["value"] == 3


def test_personal_columns_in_tabular_results_are_masked():
    result = {"kind": "tabular", "columns": ["order_id", "Phone", "customer_email"],
              "rows": [[1, "07700900123", "b@example.com"], [2, None, "c@example.com"]]}
    out = masking.apply(result, unmasked=False, personal_columns=PERSONAL)
    assert out["rows"] == [[1, "0***", "b***@example.com"], [2, None, "c***@example.com"]]
    assert out["masked_columns"] == ["Phone", "customer_email"]
    assert result["rows"][0][1] == "07700900123"            # the input is never mutated


def test_unmasked_callers_receive_the_result_as_is():
    result = {"kind": "tabular", "columns": ["customer_email"], "rows": [["d@example.com"]]}
    assert masking.apply(result, unmasked=True, personal_columns=PERSONAL) == result


@pytest.mark.parametrize("value,masked", [("ab", "***"), ("abcd", "a***"), (12345, "1***"), (None, None)])
def test_mask_value_keeps_minimal_shape(value, masked):
    assert masking.mask_value(value) == masked
