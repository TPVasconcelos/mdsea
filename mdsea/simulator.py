import logging
from dataclasses import dataclass, field
from itertools import chain, combinations
from typing import Optional

import numpy as np
from mdsea import Potential, quicker
from mdsea.config import Config
from mdsea.constants import DTYPE
from mdsea.core import SysManager
from mdsea.gen import PosGen, VelGen
from mdsea.helpers import ProgressBar, get_dt
from numpy.core.umath_tests import inner1d

log = logging.getLogger(__name__)


@dataclass
class _BaseSimulator:
    """
    _BaseSimulator object

    This object will be the basis for any simulator. It contains the
    main class variables and methods needed for any simulation.

    """

    sm: SysManager = field()

    # The integration time interval ("delta-t")
    dt: Optional[float] = field(default=None)

    # The pairwise potential
    pot: Potential = field(default=Potential.ideal())

    # Coefficient of restitution
    # "Damping factor"
    # ref: https://en.wikipedia.org/wiki/Coefficient_of_restitution
    restitution_coeff: float = field(default=1.0)

    # Force constant temperature throughout all simulation steps
    isothermal: bool = field(default=False)

    # Whether to apply a gravitational field
    gravity: bool = field(default=False)

    # TODO: implement cutoff radius
    r_cutoff: Optional[float] = field(default=None)

    def __post_init__(self) -> None:

        # Generate/set initial positions
        cgen = PosGen(ndim=self.sm.NDIM, boxlen=self.sm.LEN_BOX, nparticles=self.sm.NUM_PARTICLES)
        self.r_vec = cgen.simplecubic()

        # Generate/set initial velocities
        vgen = VelGen(ndim=self.sm.NDIM, nparticles=self.sm.NUM_PARTICLES)
        self.v_vec = vgen.mb(self.sm.MASS, self.sm.TEMP, Config.k_boltzmann)

        # Shortcut for a zeroes array with
        # shape = (NDIM, NUM_PARTICLES)
        self.ndnp_zeroes = np.zeros((self.sm.NDIM, self.sm.NUM_PARTICLES), dtype=DTYPE)

        self.dists: np.ndarray = None
        self.drunits: np.ndarray = None
        self.pairs_indexes: np.ndarray = None

        # Acceleration vectors (defined in property)
        self.acc = self.ndnp_zeroes.copy()

        # Mean kinetic and potential energies
        # They will be defined later but
        # should be of type: float
        self.mean_ke = None
        self.mean_pe = None

        # Set initial temperature
        self.temp_init = self.sm.TEMP
        self.temp = self.sm.TEMP

        # Simulation step (start at zero)
        self.step = 0

        # Set integration time interval ("delta-t")
        if self.dt is None:
            self.dt = get_dt(radius=self.sm.RADIUS_PARTICLE, mean_speed=float(quicker.norm(self.v_vec, axis=0).mean()))

        # Boundary conditions stuff
        self.apply_bc = self.apply_pbc if self.sm.PBC else self.apply_hbc

        # Flipped identity matrix
        self._FLIPID = quicker.flipid(self.sm.NDIM)
        # Apply rest coefficient ??
        self.apply_restcoeff = bool(self.restitution_coeff < 1)

        self.pbarr = ProgressBar("Simulator", self.sm.STEPS, __name__)

        # These dicts and lists keep track of the atoms' direction.
        # e.g. If they're entering or leaving a potential well.
        # Legend:
        # zero (0) stands for: "GOING IN"
        # one (1) stands for: "GOING OUT"
        self.going_where_from_well: dict = {}
        self.going_where_from_inside_particle: dict = {}
        # ---
        self.pairs_already_inside_the_well: list = []
        self.pairs_already_inside_the_particle: list = []
        # ---
        self.colliding_pairs: list = []

        # ==============================================================
        # ---  Init Pairs
        # ==============================================================

        # Generate all possible combinations of particle pairs.
        # It is straight forward to use itertools for this.
        # Then, we transform the itertools.combinations
        # object into a numpy ndarray. Not perfect...
        # numpy.fromiter is way too slow for large
        # iterables. Need a better way to do this
        self.all_pairs = np.fromiter(
            chain.from_iterable(combinations(range(self.sm.NUM_PARTICLES), 2)),
            count=self.sm.NUM_PARTICLES * (self.sm.NUM_PARTICLES - 1),
            dtype=np.int32,
        ).reshape(-1, 2)
        # shortcuts
        self._ap_0 = self.all_pairs[:, 0]
        self._ap_1 = self.all_pairs[:, 1]
        # set of indices
        self._ap_0set = np.fromiter(set(self._ap_0), dtype=int)
        self._ap_1set = np.fromiter(set(self._ap_1), dtype=int)
        # all_pairs1 sorted
        self._ap_1sort = np.sort(self._ap_1)
        self._ap_1argsort = np.argsort(self._ap_1)
        # True matrix
        self.true_matrix = np.repeat(True, self.all_pairs.shape[0])

    # ==================================================================
    # ---  File Management
    # ==================================================================

    def update_files(self) -> None:
        """Update the default datasets defines in the
        SystemManager."""

        values = [[self.r_vec], [self.v_vec], [self.mean_pe], [self.mean_ke], [self.temp]]

        for dataset, val in zip(self.sm.all_dsnames, values):
            self.sm.update_ds(dataset, val, self.step)

    # ==================================================================
    # ---  Boundary Conditions
    # ==================================================================

    def apply_pbc(self) -> None:
        """
        Apply the Periodic-Boundary-Conditions algorithm.

        One-line algorithm:
        >>> self.r_vec -= self.sm.LEN_BOX * np.floor(
        >>>     self.r_vec / self.sm.LEN_BOX)

        """
        self.r_vec[np.where(self.r_vec < 0)] += self.sm.LEN_BOX
        self.r_vec[np.where(self.r_vec > self.sm.LEN_BOX)] -= self.sm.LEN_BOX

    def apply_hbc(self) -> None:
        """ Apply the Hard-Boundary-Conditions algorithm. """

        # Particles that passed to the negative side of the boundary
        whr = np.where(self.r_vec - self.sm.RADIUS_PARTICLE < 0)
        self.r_vec[whr] = self.sm.RADIUS_PARTICLE
        self.v_vec[whr] *= -self.restitution_coeff

        # Particles that passed to the positive side of the boundary
        whr = np.where(self.r_vec + self.sm.RADIUS_PARTICLE > self.sm.LEN_BOX)
        self.r_vec[whr] = self.sm.LEN_BOX - self.sm.RADIUS_PARTICLE
        self.v_vec[whr] *= -self.restitution_coeff

    # ==================================================================
    # ---  Special Events
    # ==================================================================

    def apply_special(self) -> None:
        """ Handle special events. """
        if self.isothermal:
            self._quench(self.temp_init)
        if self.step in self.sm.QUENCH_STEP:
            self._quench(self.sm.QUENCH_T.pop(0))

    def _quench(self, temperature: float) -> None:
        self.update_temp()
        if self.temp != 0.0:
            self.v_vec *= (temperature / self.temp) ** 0.5

    # ==================================================================
    # ---  Helper Functions
    # ==================================================================

    def apply_field(self):
        """ Apply an external field. """
        # TODO: add the option for the user to pass a field.
        if self.gravity:
            self.v_vec[-1] -= Config.gravity_acceleration * self.dt

    @property
    def com(self):
        """ Centre of mass. """
        if self.sm.PBC:
            log.warning("COM computation not available for PBC yet!")
            return
        return np.mean(self.r_vec, axis=1)

    @property
    def rog(self) -> float:
        """Radius of gyration. Mathematically expressed as the
        root-mean-squared of the distances to the centre-of-mass."""

        # Calculate the separation distance
        # vectors from the centre-of-mass
        dr_vecs = np.stack(self.r_vec, axis=-1) - self.com

        # If the vectors are bigger than half of the
        # box length, reflect the relative distance
        # to respect periodic boundary conditions.
        if self.sm.PBC:
            dr_vecs -= np.rint(dr_vecs / self.sm.LEN_BOX) * self.sm.LEN_BOX

        # Root-mean-squared of the distances to the centre-of-mass
        return np.sqrt(np.mean(quicker.norm(dr_vecs, axis=1) ** 2))

    def update_temp(self) -> Optional[float]:
        """ Update the system's temperature. """
        if self.mean_ke is None:
            log.warning("Cannot update the temperature without first" " evaluating the mean kinetic energy.")
            return None
        self.temp = (2 / 3) * self.mean_ke / Config.k_boltzmann
        return self.temp

    def update_mean_ke(self) -> np.ndarray:
        """ Update the mean kinetic energy. """
        vvect = np.stack(self.v_vec, axis=-1)
        self.mean_ke = 0.5 * self.sm.MASS * inner1d(vvect, vvect).mean()
        return self.mean_ke

    def update_mean_pe(self) -> np.ndarray:
        """ Update the mean potential energy. """
        self.mean_pe = np.add.reduce(self.pot.potential(self.dists)) / self.sm.NUM_PARTICLES
        return self.mean_pe

    def update_energies(self) -> None:
        """ Update the mean kinetic and potential energies. """
        self.update_mean_pe()
        self.update_mean_ke()

    def update_dists(self, radius: Optional[float] = None, where: str = "inside") -> np.ndarray:
        """Get the pairs inside/outside a given radial distance (cutoff
         radius), where the 'where' parameter has to be either  'inside'
        or 'outside', respectively."""

        # Transpose position vector
        r_vecs = np.stack(self.r_vec, axis=-1)

        # Calculate the pairwise separation distance vectors
        dr_vecs = r_vecs[self._ap_1] - r_vecs[self._ap_0]

        # If the vectors are bigger than half of the
        # box length, reflect the relative distance
        # to respect periodic boundary conditions.
        if self.sm.PBC:
            dr_vecs -= np.rint(dr_vecs / self.sm.LEN_BOX) * self.sm.LEN_BOX

        # Calculate euclidean distances
        self.dists = quicker.norm(dr_vecs, axis=1)

        if radius is None:
            self.pairs_indexes = self.true_matrix.copy()
            self.drunits = dr_vecs / self.dists[:, np.newaxis]
            return self.dists

        if where == "inside":
            self.pairs_indexes = self.dists < radius
        elif where == "outside":
            self.pairs_indexes = self.dists > radius
        else:
            raise ValueError(f"'{where}' is not a valid value for 'where'. " f"Try 'inside' or 'outside' instead.")

        self.dists = self.dists[self.pairs_indexes]

        self.drunits = dr_vecs[self.pairs_indexes] / self.dists[:, np.newaxis]

        return self.dists

    def update_acc(self, radius: float = None) -> np.ndarray:
        """Returns (and/or update) the acceleration vectors for
        particles under a given pairwise potential force and within a
        certain cutoff radius."""
        self.update_dists(radius=radius)

        acc = self.drunits * self.pot.force(self.dists[:, np.newaxis]) / self.sm.MASS

        self.acc = self.ndnp_zeroes.copy()

        self.acc[:, self._ap_0set] += np.array([np.bincount(self._ap_0, acc[:, i]) for i in range(self.sm.NDIM)])

        self.acc[:, self._ap_1set] -= np.array(
            [np.bincount(self._ap_1sort, acc[self._ap_1argsort][:, i]) for i in range(self.sm.NDIM)]
        )[:, 1:]

        return self.acc

    @property
    def pairs(self) -> np.ndarray:
        """ Return pairs within a cutoff. See update_pairs() """
        return self.all_pairs[self.pairs_indexes]

    def update_pairs(self, radius: Optional[float] = None, where: str = "inside") -> np.ndarray:
        """ Updates particle pairs within a certain cutoff radius."""
        self.update_dists(radius=radius, where=where)

        return zip(self.all_pairs[self.pairs_indexes], self.dists, self.drunits)


# TODO: Update whole class in accordance with self.*coords
# class _StepPotentialSolver(_BaseSimulator):
#     def get_position_vector(self):
#         return np.stack(self.r_vec, axis=-1)
#
#     def get_vel_vector(self):
#         return np.stack(self.v_vec, axis=-1)
#
#     @staticmethod
#     def unit_vector(vector, norm=None):
#         if norm is None:
#             norm = sqrt(np.dot(vector, vector))
#         return vector / norm
#
#     def pair_did_not_actually_leave_the_well(self, i, j):
#         try:
#             # False <=> 0 <=> Going IN
#             # True  <=> 1 <=> Going OUT
#             return self.going_where_from_well[(i, j)]
#         except KeyError:
#             return False
#
#     def pair_did_not_actually_leave_inside_particle(self, i, j):
#         try:
#             # False <=> 0 <=> Going IN
#             # True  <=> 1 <=> Going OUT
#             return self.going_where_from_inside_particle[(i, j)]
#         except KeyError:
#             return False
#
#     def separate_colliding_pairs(self, i, j):
#         x_j, x_i = self.x[self.step][j], self.x[self.step][i]
#         y_j, y_i = self.y[self.step][j], self.y[self.step][i]
#         delta_r = np.array([(x_j - x_i), (y_j - y_i)])
#         unit_delta_r = delta_r / np.linalg.norm(delta_r)
#         positions_vector_i = np.array((x_i, y_i)) - (
#                 SIGMA * unit_delta_r - delta_r) / 2
#         positions_vector_j = np.array((x_j, y_j)) + (
#                 SIGMA * unit_delta_r - delta_r) / 2
#         self.x[self.step][i], self.y[self.step][i] = positions_vector_i
#         self.x[self.step][j], self.y[self.step][j] = positions_vector_j
#
#     def apply_hard_sphere_collision(self, i, j):
#         r_i, r_j = self.get_position_vector(i=i), self.get_position_vector(i=j)
#         v_i, v_j = self.get_vel_vector(i=i), self.get_vel_vector(i=j)
#         # relative position and velocity vectors
#         r_rel = r_i - r_j
#         v_rel = v_i - v_j
#         # momentum vector of the center of mass
#         v_cm = (v_i + v_j) / 2.
#         # collisions of perfect elastic hard spheres
#         rr_rel = np.dot(r_rel, r_rel)
#         vr_rel = np.dot(v_rel, r_rel)
#         v_rel = 2. * r_rel * vr_rel / rr_rel - v_rel
#         # assign new velocity vectors
#         self.vx[self.step][i], self.vy[self.step][i] = v_cm - v_rel / 2.
#         self.vx[self.step][j], self.vy[self.step][j] = v_cm + v_rel / 2.
#
#     def apply_square_well_attraction(self, i, j):
#         # positions
#         x_i, x_j = self.x[self.step][i], self.x[self.step][j]
#         y_i, y_j = self.y[self.step][i], self.y[self.step][j]
#         # delta_r points from i to j
#         delta_r = np.array([(x_j - x_i), (y_j - y_i)])
#         v_extra_pull = (EPSILON / self.MASS) * self.unit_vector(delta_r)
#         # velocities
#         v_i = self.get_vel_vector(i=i)
#         v_j = self.get_vel_vector(i=j)
#         # velocities squared
#         v_i_squared = (np.linalg.norm(v_i) ** 2) * self.unit_vector(v_i)
#         v_j_squared = (np.linalg.norm(v_j) ** 2) * self.unit_vector(v_j)
#         # combine velocities
#         v_i = v_i_squared + (np.sqrt(2) * v_extra_pull)
#         v_j = v_j_squared - (np.sqrt(2) * v_extra_pull)
#         self.vx[self.step][i], self.vy[self.step][i] = np.sqrt(
#             np.linalg.norm(v_i)) * self.unit_vector(v_i)
#         self.vx[self.step][j], self.vy[self.step][j] = np.sqrt(
#             np.linalg.norm(v_j)) * self.unit_vector(v_j)
#
#     def apply_top_hat_repulsion(self, i, j):
#         # positions
#         x_i, x_j = self.x[self.step][i], self.x[self.step][j]
#         y_i, y_j = self.y[self.step][i], self.y[self.step][j]
#         # delta_r points from i to j
#         delta_r = np.array([(x_j - x_i), (y_j - y_i)])
#         delta_r_unit = delta_r / np.linalg.norm(delta_r)
#         # velocities
#         v_i = self.get_vel_vector(i=i)
#         v_j = self.get_vel_vector(i=j)
#         # extra pull in i <-> j direction
#         v_extra_pull = - np.sqrt(2 * TOP_HAT_PARAM / self.MASS) * delta_r_unit
#         v_i += v_extra_pull / 2.
#         v_j -= v_extra_pull / 2.
#         # assign new velocity vectors
#         self.vx[self.step][i], self.vy[self.step][i] = v_i
#         self.vx[self.step][j], self.vy[self.step][j] = v_j
#
#     def particle_has_enough_kinetic_energy(self, i, j):
#         v_i, v_j = np.array([self.vx[self.step][i], self.vy[self.step][i]]), \
#                    np.array([self.vx[self.step][j], self.vy[self.step][j]])
#         v_rel_norm = np.linalg.norm(v_i - v_j)
#         k_energy = 0.5 * self.MASS * v_rel_norm ** 2
#         if k_energy > EPSILON:
#             return True
#         return False
#
#     def hard_sphere(self):
#         colliding_pairs = self.update_pairs(SIGMA)
#         for i, j in colliding_pairs:
#             self.separate_colliding_pairs(i, j)
#             self.apply_hard_sphere_collision(i, j)
#
#     def square_well(self):
#         # apply Hard Sphere First
#         self.hard_sphere()
#         if self.step != 0:
#             for pair in self.update_pairs(R_SQUAREWELL * SIGMA):
#                 i, j = pair[0], pair[1]
#                 if self.pair_did_not_actually_leave_the_well(i, j):
#                     """This means that, if the particle was about to leave the well
#                     in the previous step but actually got bounced back inside...
#                     the particle never actually left the well but it is still
#                     'marked' as outside the potential in the previous step..."""
#                     continue
#                 elif (i, j) not in self.pairs_already_inside_the_well:
#                     # If this pair was not inside the potential
#                     # in the previous step, pull them together.
#                     self.going_where_from_well[(i, j)] = 0  # IN
#                     self.apply_square_well_attraction(i, j)
#             for i, j in self.get_pairs_outside_radius(R_SQUAREWELL * SIGMA):
#                 if (i, j) in self.pairs_already_inside_the_well:
#                     self.going_where_from_well[(i, j)] = 1  # OUT
#                     # self.apply_square_well_attraction(i, j)
#                     # """
#                     if self.particle_has_enough_kinetic_energy(i, j):
#                         # If this pair was inside the potential in the
#                         # previous step, pull them together again.
#                         # This is because the particles are only allowed
#                         # out if they can pass over the potential step!
#                         self.apply_square_well_attraction(i, j)
#                     else:
#                         self.apply_hard_sphere_collision(i, j)
#                         # """
#
#     def top_hat(self):
#         # Penetrable Sphere
#         for i, j in self.update_pairs(SIGMA):
#             if self.pair_did_not_actually_leave_inside_particle(i, j):
#                 # TODO: Fix this ugly fix...
#                 continue
#             elif (i, j) not in self.pairs_already_inside_the_particle:
#                 self.apply_top_hat_repulsion(i, j)
#                 self.going_where_from_inside_particle[(i, j)] = 0  # IN
#         for i, j in self.get_pairs_outside_radius(SIGMA):
#             if (i, j) in self.pairs_already_inside_the_particle:
#                 self.apply_top_hat_repulsion(i, j)
#                 self.going_where_from_inside_particle[(i, j)] = 1  # OUT
#         self.pairs_already_inside_the_particle = self.update_pairs(
#             SIGMA)
#
#         # Entering and exiting the well
#         for i, j in self.update_pairs(R_SQUAREWELL * SIGMA):
#             if self.pair_did_not_actually_leave_inside_particle(i, j):
#                 # TODO: Fix this ugly fix...
#                 # This means that, if the particle was about to leave the well
#                 # in the previous step but actually got bounced back inside...
#                 # the particle never actually left the well but it is still
#                 # 'marked' as outside the potential in the previous step...
#                 continue
#             elif (i, j) not in self.pairs_already_inside_the_well:
#                 # If this pair was not inside the potential
#                 # in the previous step, pull them together.
#                 self.apply_square_well_attraction(i, j)
#                 self.going_where_from_well[(i, j)] = 0  # IN
#         for i, j in self.get_pairs_outside_radius(R_SQUAREWELL * SIGMA):
#             if (i, j) in self.pairs_already_inside_the_well:
#                 # If this pair was inside the potential in the
#                 # previous step, pull them together again.
#                 # This is because the particles are only allowed
#                 # out if they can pass over the potential step!
#                 self.apply_square_well_attraction(i, j)
#                 self.going_where_from_well[(i, j)] = 1  # OUT
#         self.pairs_already_inside_the_well = self.update_pairs(
#             R_SQUAREWELL * SIGMA)


@dataclass
class ContinuousPotentialSolver(_BaseSimulator):

    algorithm: str = field(default="verlet")

    def __post_init__(self) -> None:
        super().__post_init__()

        algorithms_tbl = {
            "verlet": self.algorithm_verlet,
            "simple": self.algorithm_simple,
        }

        try:
            self.apply_algorithm = algorithms_tbl[self.algorithm]
        except KeyError:
            msg = f"Algorithm '{self.algorithm}' not found " f"in {tuple(algorithms_tbl.keys())}"
            raise KeyError(msg)

    def algorithm_simple(self):
        """ Simple Verlet Algorithm. """
        # Update position: t + dt
        self.r_vec += self.v_vec * self.dt
        # Update velocity: t + dt
        self.v_vec += 0.5 * self.update_acc() * self.dt

    def algorithm_verlet(self):
        """ Verlet Algorithm. """
        # Update velocity: t + dt/2
        self.v_vec += 0.5 * self.update_acc() * self.dt
        # Update position: t + dt
        self.r_vec += self.v_vec * self.dt
        # Update velocity: t + dt
        self.v_vec += 0.5 * self.update_acc() * self.dt

    def apply_collision_damping(self):
        """ Apply a dissipative force for every particle collision. """
        raise NotImplementedError
        # TODO: review this whole method!
        # new_collpairs = []
        # self.update_pairs(2 * self.sm.RADIUS_PARTICLE)
        # for (i, j), dist, dr_unit in zip(self.pairs, self.dists, self.drunits):
        #     new_collpairs.append((i, j))
        #     if (i, j) not in self.colliding_pairs:
        #         v_factor = self.restitution_coeff * np.dot(self._FLIPID, dr_unit)
        #         self.v_vec[:, i] *= v_factor
        #         self.v_vec[:, j] *= v_factor
        # self.colliding_pairs = new_collpairs

    def advance(self):
        """ Advance the simulation one step. """
        self.apply_bc()
        self.apply_algorithm()
        self.apply_field()
        if self.apply_restcoeff:
            self.apply_collision_damping()
        self.apply_special()
        self.update_energies()

    def run_simulation(self) -> None:
        """ Run simulation. """

        simrange = range(self.step, self.sm.STEPS)

        # Make sure that we update
        # the files at least once
        if len(simrange) == 0:
            self.update_files()

        # ---  the actual simulation  ---
        self.pbarr.set_start()

        for self.step in simrange:
            self.pbarr.log_progress(self.step)
            self.advance()
            self.update_files()

        self.pbarr.set_finish()
        self.pbarr.log_duration()
        # ---  the actual simulation  ---
