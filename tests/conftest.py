import pytest
from recovery import solver


@pytest.fixture(autouse=True)
def fast_solver(monkeypatch):
    # Smaller deterministic budget keeps the suite quick; proofs on these small fixtures still complete.
    monkeypatch.setattr(solver, 'DET_TIME', 0.15)
