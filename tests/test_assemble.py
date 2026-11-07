from broll.assemble import _fit_pool
from broll.project import Line


def test_fit_pool_positions_and_shortfall():
    line = Line(t="x")
    assert _fit_pool(0.0, 10.0, 4.0, line) == (3.0, 0.0)
    assert _fit_pool(2.0, 10.0, 4.0, Line(t="x", fit="start")) == (2.0, 0.0)
    assert _fit_pool(0.0, 10.0, 4.0, Line(t="x", fit="end")) == (6.0, 0.0)
    assert _fit_pool(0.0, 10.0, 4.0, Line(t="x", frm=9.0)) == (6.0, 0.0)   # the in-point is clamped to fit
    assert _fit_pool(0.0, 3.0, 4.0, line) == (0.0, 1.0)                     # the missing second is a freeze frame
