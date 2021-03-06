from mdsea.core import SysManager
from mdsea.helpers import setup_logging
from mdsea.potentials import Potential
from mdsea.simulator import ContinuousPotentialSolver

setup_logging(level="DEBUG")

# Calculate the 'number of steps'  ---
SECONDS = 2
FRAME_STEP = 5
FRAMES_PER_SECOND = 24
STEPS = int(SECONDS * FRAMES_PER_SECOND * FRAME_STEP)

# Potential function ( /python-class )
pot = Potential.boundedmie(a=0.2, epsilon=1, sigma=1, m=12, n=6)
p_radius = pot.kwargs["sigma"] / 2

# Instantiate system  ---
NDIM = 3
sm = SysManager.new(
    # ID (optional)
    simid="_mdsea_docs_example",
    # Mandatory fields
    ndim=NDIM,
    num_particles=3 ** NDIM,
    vol_fraction=0.1,
    radius_particle=p_radius,
    # Optional fields
    pbc=False,
    temp=1,
    # mass=1,
    # gravity=True,
    # delta_t=None,
    steps=STEPS,
    # isothermal=True,
    # quench_temps=[],
    # quench_steps=[],
    # quench_timings=[],
    # restitution_coeff=0.4,
    # reduced_units=False
)

# Instantiate simulation  ---
sim = ContinuousPotentialSolver(
    sm=sm,
    pot=pot,
    # r_cutoff=2.5 * pot.potminimum()
)

# Run the simulation  ---
sim.run_simulation()
