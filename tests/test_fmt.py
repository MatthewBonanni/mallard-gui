"""Numbers as the UI shows them: comma digit groups; scientific notation and decimals as they were."""

from __future__ import annotations

from mallard_gui.ui.fmt import grouped, number


def test_whole_numbers_group_with_commas_and_the_rest_keeps_its_form() -> None:
    assert grouped(2097152) == "2,097,152" and grouped(-1234) == "-1,234" and grouped(999) == "999"
    assert number(1234567) == "1,234,567"
    assert number(1.5e6) == "1.500e+06" and number(0.01881) == "0.01881" and number(1234.5) == "1234"
