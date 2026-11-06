"""Sums are worked out, not looked up."""
import pytest

from jeeves.functions.partials.calc import MathError, evaluate, pretty, to_expression


@pytest.mark.parametrize("said, want", [
    ("What is 2+2?", "4"), ("whats two plus two", "4"), ("fifteen percent of eighty", "12"),
    ("square root of 144", "12"), ("12 times 7 divided by 3", "28"), ("2 to the power of 10", "1,024"),
    ("one thousand two hundred and five minus five", "1,200"), ("5 squared", "25"),
    ("what is twenty-one times two", "42"), ("three point five plus one", "4.5"),
])
def test_spoken_sums(said, want):
    assert pretty(evaluate(to_expression(said))) == want


@pytest.mark.parametrize("said", ["what time is it", "what is the capital of france", "set a timer for 5 minutes",
                                  "what is 7", "skip 30 seconds"])
def test_not_sums(said):
    assert to_expression(said) is None


def test_no_eval_tricks():
    for bad in ["__import__('os')", "2 ** 100000"]:
        with pytest.raises(MathError):
            evaluate(bad)
    with pytest.raises(MathError):
        evaluate("10 / 0")


def test_jeeves_answers_a_sum_without_research(engine):
    res = engine.handle_text("Jeeves, what's 2+2?", wait=True)
    e = engine.history.get(res["id"])
    assert e["function"] == "math" and e["response"].startswith("4")
    assert not any(t["kind"] == "call" and t.get("function") == "web_search" for t in e["trace"])


def test_research_answers_dont_talk_about_their_sources():
    from jeeves.functions.builtins import tidy_answer
    friend = ('The question "What is 2+2?" is a basic arithmetic problem, but none of the provided sources '
              'discuss mathematical operations or answer this question. The sources are focused on gaming '
              'guides, RPG design, and mental health topics. \n\n[Answer] 2+2 equals 4.  \n[Detail] The sources '
              'do not address mathematical calculations or arithmetic operations.')
    assert tidy_answer(friend) == "2+2 equals 4."
    assert tidy_answer("4.\n\nThe answer is 4. This is a basic arithmetic calculation. [4]") == "4. [4]"
    assert tidy_answer("The sources do not say when it releases.") == "The sources do not say when it releases."
