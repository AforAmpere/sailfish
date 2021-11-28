from logging import getLogger
from contextlib import nullcontext
from sailfish.library import Library
from sailfish.system import get_array_module
from sailfish.subdivide import subdivide

logger = getLogger(__name__)


"""
Holds the array buffer state for the solution on a subset of the solution
domain.
"""


class Patch:
    def __init__(
        self,
        index_range,
        grid_spacing,
        primitive,
        time,
        lib,
        xp,
        coordinates="cartesian",
    ):
        i0, i1 = index_range
        self.lib = lib
        self.xp = xp
        self.num_zones = primitive.shape[0]
        self.faces = self.xp.array([i * grid_spacing for i in range(i0, i1 + 1)])
        self.coordinates = dict(cartesian=0, spherical=1)[coordinates]
        self.scale_factor_initial = 1.0
        self.scale_factor_derivative = 0.0
        self.time = self.time0 = time

        with self.execution_context():
            self.primitive1 = self.xp.array(primitive)
            self.conserved0 = self.primitive_to_conserved(self.primitive1)
            self.conserved1 = self.conserved0.copy()
            self.conserved2 = self.conserved0.copy()

    def execution_context(self):
        return nullcontext()

    def primitive_to_conserved(self, primitive):
        with self.execution_context():
            conserved = self.xp.zeros_like(primitive)
            self.lib.srhd_1d_primitive_to_conserved(
                self.num_zones,
                self.faces,
                primitive,
                conserved,
                self.scale_factor(),
                self.coordinates,
            )
            return conserved

    def recompute_primitive(self):
        with self.execution_context():
            self.lib.srhd_1d_conserved_to_primitive(
                self.num_zones,
                self.faces,
                self.conserved1,
                self.primitive1,
                self.scale_factor(),
                self.coordinates,
            )

    def advance_rk(self, rk_param, dt):
        self.recompute_primitive()

        with self.execution_context():
            self.lib.srhd_1d_advance_rk(
                self.num_zones,
                self.faces,
                self.conserved0,
                self.primitive1,
                self.conserved1,
                self.conserved2,
                self.scale_factor_initial,
                self.scale_factor_derivative,
                self.time,
                rk_param,
                dt,
                self.coordinates,
            )
        self.time = self.time0 * rk_param + (self.time0 + dt) * (1.0 - rk_param)
        self.conserved1, self.conserved2 = self.conserved2, self.conserved1

    def scale_factor(self):
        return self.scale_factor_initial + self.scale_factor_derivative * self.time

    def new_timestep(self):
        self.time0 = self.time
        self.conserved0[...] = self.conserved1[...]

    @property
    def primitive(self):
        self.recompute_primitive()
        return self.primitive1


"""
Adapter class to drive the srhd_1d C extension module.
"""


class Solver:
    def __init__(
        self,
        primitive,
        time,
        num_patches=1,
        mode="cpu",
        boundary_condition="outflow",
        coordinates="cartesian",
    ):
        num_zones = primitive.shape[0]
        xp = get_array_module(mode)
        dx = 1.0 / num_zones
        ng = 2  # number of guard zones
        nq = 4  # number of conserved quantities
        lib = Library(__file__, mode=mode, debug=False)

        logger.info(f"initiate with time={time:0.4f}")
        logger.info(f"subdivide grid over {num_patches} patches")
        logger.info(f"use {boundary_condition} boundary condition")
        logger.info(f"use {coordinates} coordinates")

        self.boundary_condition = boundary_condition
        self.num_guard = ng
        self.num_cons = nq
        self.num_zones = num_zones
        self.xp = xp
        self.patches = []

        for (a, b) in subdivide(self.num_zones, num_patches):
            prim = xp.zeros([b - a + 2 * ng, nq])
            prim[ng:-ng] = primitive[a:b]
            self.patches.append(
                Patch((a - ng, b + ng), dx, prim, time, lib, xp, coordinates)
            )

    def advance_rk(self, rk_param, dt):
        ng = self.num_guard
        patches = self.patches
        num_patches = len(patches)
        for i in range(num_patches):
            pl = self.patches[(i + num_patches - 1) % num_patches]
            p0 = self.patches[i]
            pr = self.patches[(i + num_patches + 1) % num_patches]
            self.set_bc(pl.primitive1, p0.primitive1, pr.primitive1, i)
            self.set_bc(pl.conserved1, p0.conserved1, pr.conserved1, i)

        for patch in self.patches:
            patch.advance_rk(rk_param, dt)

    def set_bc(self, al, a0, ar, index):
        ng = self.num_guard
        bc = self.boundary_condition

        if bc == "periodic":
            a0[:+ng] = al[-2 * ng : -ng]
            a0[-ng:] = ar[+ng : +2 * ng]
        elif bc == "outflow":
            if index == 0:
                a0[:+ng] = a0[+ng : +2 * ng]
            else:
                a0[:+ng] = al[-2 * ng : -ng]
            if index == len(self.patches) - 1:
                a0[-ng:] = a0[-2 * ng : -ng]
            else:
                a0[-ng:] = ar[+ng : +2 * ng]
        else:
            raise ValueError("boundary condition must be 'periodic | outflow'")

    def new_timestep(self):
        for patch in self.patches:
            patch.new_timestep()

    @property
    def primitive(self):
        nz = self.num_zones
        ng = self.num_guard
        nq = self.num_cons
        np = len(self.patches)
        primitive = self.xp.zeros([self.num_zones, nq])
        for (a, b), patch in zip(subdivide(nz, np), self.patches):
            primitive[a:b] = patch.primitive[ng:-ng]
        return primitive

    @property
    def time(self):
        return self.patches[0].time
