from collections.abc import MutableMapping
from collections import ChainMap
from loguru import logger
from numpy import linspace, meshgrid, zeros, logical_not
from numpy.typing import NDArray
from new_kernels import (
    kernel,
    kernel_class,
    perf_time_sequence,
    configure_kernel_module,
    device,
)
from lib_euler import prim_to_cons, cons_to_prim, riemann_hlle
from configuration import configurable, all_schemas


@device
def plm_minmod(yl: float, yc: float, yr: float, plm_theta: float):
    R"""
    #define min2(a, b) ((a) < (b) ? (a) : (b))
    #define min3(a, b, c) min2(a, min2(b, c))
    #define sign(x) copysign(1.0, x)
    #define minabs(a, b, c) min3(fabs(a), fabs(b), fabs(c))

    DEVICE double plm_minmod(
        double yl,
        double yc,
        double yr,
        double plm_theta)
    {
        double a = (yc - yl) * plm_theta;
        double b = (yr - yl) * 0.5;
        double c = (yr - yc) * plm_theta;
        return 0.25 * fabs(sign(a) + sign(b)) * (sign(a) + sign(c)) * minabs(a, b, c);
    }
    """


@kernel_class
class FluxPerFaceSolver:
    """
    Solves 1d Euler equation with per-face fluxing at 1st or 2nd order.

    This solver supports 4 configurations -- with/without gradient estimation
    (via PLM) and with/without explicit Runge-Kutta support. Fluxing is done
    per-face, meaning that a Riemann problem is solved only once per face. This
    approach is best in compute-limited settings because it minimizes flops.
    """

    def __init__(self, runge_kutta=False, plm=False):
        self.runge_kutta = runge_kutta
        self.plm = plm

    @property
    def define_macros(self):
        return dict(DIM=1, RUNGE_KUTTA=int(self.runge_kutta), PLM=int(self.plm))

    @property
    def device_funcs(self):
        d = [prim_to_cons, cons_to_prim, riemann_hlle]
        if self.plm:
            d.append(plm_minmod)
        return d

    @kernel
    def compute_godunov_fluxes(
        self,
        p: NDArray[float],
        f: NDArray[float],
        plm_theta: float,
        ni: int = None,
    ):
        R"""
        KERNEL void compute_godunov_fluxes(double *p, double *f, double plm_theta, int ni)
        {
            FOR_RANGE_1D(1, ni - 2)
            {
                double pm[NCONS];
                double pp[NCONS];
                double *fh = &f[NCONS * (i + 1)];

                #if PLM == 0

                double *pc = &p[NCONS * (i + 0)];
                double *pr = &p[NCONS * (i + 1)];

                for (int q = 0; q < NCONS; ++q)
                {
                    pm[q] = pc[q];
                    pp[q] = pr[q];
                }
                #else

                double *pl = &p[NCONS * (i - 1)];
                double *pc = &p[NCONS * (i + 0)];
                double *pr = &p[NCONS * (i + 1)];
                double *ps = &p[NCONS * (i + 2)];

                for (int q = 0; q < NCONS; ++q)
                {
                    pm[q] = pc[q] + 0.5 * plm_minmod(pl[q], pc[q], pr[q], plm_theta);
                    pp[q] = pr[q] - 0.5 * plm_minmod(pc[q], pr[q], ps[q], plm_theta);
                }
                #endif

                riemann_hlle(pm, pp, fh, 1);
            }
        }
        """
        return p.shape[0], (p, f, plm_theta, p.shape[0])

    @kernel
    def update_prim(
        self,
        p: NDArray[float],
        f: NDArray[float],
        urk: NDArray[float],
        dt: float,
        dx: float,
        rk: float,
        ni: int = None,
    ):
        R"""
        KERNEL void update_prim(
            double *p,
            double *f,
            double *urk,
            double dt,
            double dx,
            double rk,
            int ni)
        {
            FOR_RANGE_1D(2, ni - 2)
            {
                double uc[3];
                double *pc = &p[3 * (i + 0)];
                double *fm = &f[3 * (i + 0)];
                double *fp = &f[3 * (i + 1)];

                prim_to_cons(pc, uc);

                for (int q = 0; q < 3; ++q)
                {
                    uc[q] -= (fp[q] - fm[q]) * dt / dx;
                    #if RUNGE_KUTTA == 1
                    uc[q] *= (1.0 - rk);
                    uc[q] += rk * urk[3 * i + q];
                    #endif
                }
                cons_to_prim(uc, pc);
            }
        }
        """
        return p.size // 3, (p, f, urk, dt, dx, rk, p.size // 3)

    @kernel
    def prim_to_cons_array(self, p: NDArray[float], u: NDArray[float], ni: int = None):
        R"""
        KERNEL void prim_to_cons_array(double *p, double *u, int ni)
        {
            FOR_RANGE_1D(1, ni - 1)
            {
                prim_to_cons(&p[NCONS * i], &u[NCONS * i]);
            }
        }
        """
        return p.size // 3, (p, u, p.size // 3)


@kernel_class
class FluxPerZoneSolver:
    """
    Solves 1d Euler equation with per-zone fluxing at 1st or 2nd order.

    This solver supports 4 configurations -- with/without gradient estimation
    (via PLM) and with/without explicit Runge-Kutta support. Fluxing is done
    per-zone, meaning that a Riemann problem is solved with two-fold redundancy;
    both zones sharing a given face compute the Godunov flux through it. This
    approach is best in memory bandwidth-limited settings because it minimizes
    access to global memory.
    """

    def __init__(self, runge_kutta=False, plm=False):
        self.runge_kutta = runge_kutta
        self.plm = plm

    @property
    def define_macros(self):
        return dict(DIM=1, RUNGE_KUTTA=int(self.runge_kutta), PLM=int(self.plm))

    @property
    def device_funcs(self):
        d = [prim_to_cons, cons_to_prim, riemann_hlle]
        if self.plm:
            d.append(plm_minmod)
        return d

    @kernel
    def update_prim(
        self,
        prd: NDArray[float],  # read-from primitive
        pwr: NDArray[float],  # write-to-primitive
        urk: NDArray[float],  # u at time-level n
        dt: float,  # time step dt
        dx: float,  # grid spacing dx
        rk: float,  # RK parameter
        plm_theta: float,
        ni: int = None,
    ):
        R"""
        KERNEL void update_prim(
            double *prd,
            double *pwr,
            double *urk,
            double dt,
            double dx,
            double rk,
            double plm_theta,
            int ni)
        {
            FOR_RANGE_1D(2, ni - 2)
            {
                double uc[NCONS];
                double fm[NCONS];
                double fp[NCONS];
                double plp[NCONS];
                double pcm[NCONS];
                double pcp[NCONS];
                double prm[NCONS];

                #if PLM == 0

                double *pl = &prd[NCONS * (i - 1)];
                double *pc = &prd[NCONS * (i + 0)];
                double *pr = &prd[NCONS * (i + 1)];

                for (int q = 0; q < NCONS; ++q)
                {
                    plp[q] = pl[q];
                    pcm[q] = pc[q];
                    pcp[q] = pc[q];
                    prm[q] = pr[q];
                }
                #else

                double *pk = &prd[NCONS * (i - 2)];
                double *pl = &prd[NCONS * (i - 1)];
                double *pc = &prd[NCONS * (i + 0)];
                double *pr = &prd[NCONS * (i + 1)];
                double *ps = &prd[NCONS * (i + 2)];

                for (int q = 0; q < NCONS; ++q)
                {
                    double gl = plm_minmod(pk[q], pl[q], pc[q], plm_theta);
                    double gc = plm_minmod(pl[q], pc[q], pr[q], plm_theta);
                    double gr = plm_minmod(pc[q], pr[q], ps[q], plm_theta);

                    plp[q] = pl[q] + 0.5 * gl;
                    pcm[q] = pc[q] - 0.5 * gc;
                    pcp[q] = pc[q] + 0.5 * gc;
                    prm[q] = pr[q] - 0.5 * gr;
                }
                #endif

                riemann_hlle(plp, pcm, fm, 1);
                riemann_hlle(pcp, prm, fp, 1);
                prim_to_cons(pc, uc);

                for (int q = 0; q < NCONS; ++q)
                {
                    uc[q] -= (fp[q] - fm[q]) * dt / dx;
                    #if RUNGE_KUTTA == 1
                    uc[q] *= (1.0 - rk);
                    uc[q] += rk * urk[NCONS * i + q];
                    #endif
                }
                cons_to_prim(uc, &pwr[NCONS * i]);
            }
        }
        """
        return prd.shape[0], (prd, pwr, urk, dt, dx, rk, plm_theta, prd.shape[0])

    @kernel
    def prim_to_cons_array(self, p: NDArray[float], u: NDArray[float], ni: int = None):
        R"""
        KERNEL void prim_to_cons_array(double *p, double *u, int ni)
        {
            FOR_RANGE_1D(1, ni - 1)
            {
                prim_to_cons(&p[NCONS * i], &u[NCONS * i]);
            }
        }
        """
        return p.size // 3, (p, u, p.size // 3)


@kernel_class
class FluxPerZoneSolver2D:
    """
    Solves 2d Euler equation with per-zone fluxing at 1st or 2nd order.

    This solver supports 4 configurations -- with/without gradient estimation
    (via PLM) and with/without explicit Runge-Kutta support. Fluxing is done
    per-zone, meaning that a Riemann problem is solved with two-fold redundancy;
    both zones sharing a given face compute the Godunov flux through it. This
    approach is best in memory bandwidth-limited settings because it minimizes
    access to global memory.
    """

    def __init__(self, runge_kutta=False, plm=False):
        self.runge_kutta = runge_kutta
        self.plm = plm

    @property
    def define_macros(self):
        return dict(DIM=2, RUNGE_KUTTA=int(self.runge_kutta), PLM=int(self.plm))

    @property
    def device_funcs(self):
        d = [prim_to_cons, cons_to_prim, riemann_hlle]
        if self.plm:
            d.append(plm_minmod)
        return d

    @kernel
    def update_prim(
        self,
        prd: NDArray[float],  # read-from primitive
        pwr: NDArray[float],  # write-to-primitive
        urk: NDArray[float],  # u at time-level n
        dt: float,  # time step dt
        dx: float,  # grid spacing (both directions)
        rk: float,  # RK parameter
        plm_theta: float,
        ni: int = None,  # number of zones on i-axis (includes guard)
        nj: int = None,  # number of zones on j-axis (includes guard)
    ):
        R"""
        KERNEL void update_prim(
            double *prd,
            double *pwr,
            double *urk,
            double dt,
            double dx,
            double rk,
            double plm_theta,
            int ni,
            int nj)
        {
            FOR_RANGE_2D(2, ni - 2, 2, nj - 2)
            {
                int si = NCONS * nj;
                int sj = NCONS;

                double ucc[NCONS];
                double fm[NCONS];
                double fp[NCONS];
                double gm[NCONS];
                double gp[NCONS];
                double pilp[NCONS];
                double picm[NCONS];
                double picp[NCONS];
                double pirm[NCONS];
                double pjlp[NCONS];
                double pjcm[NCONS];
                double pjcp[NCONS];
                double pjrm[NCONS];

                #if PLM == 0

                double *pcc = &prd[(i + 0) * si + (j + 0) * sj];
                double *plc = &prd[(i - 1) * si + (j + 0) * sj];
                double *prc = &prd[(i + 1) * si + (j + 0) * sj];
                double *pcl = &prd[(i + 0) * si + (j - 1) * sj];
                double *pcr = &prd[(i + 0) * si + (j + 1) * sj];

                for (int q = 0; q < NCONS; ++q)
                {
                    pilp[q] = plc[q];
                    picm[q] = pcc[q];
                    picp[q] = pcc[q];
                    pirm[q] = prc[q];
                    pjlp[q] = pcl[q];
                    pjcm[q] = pcc[q];
                    pjcp[q] = pcc[q];
                    pjrm[q] = pcr[q];
                }
                #else

                double *pcc = &prd[(i + 0) * si + (j + 0) * sj];
                double *pkc = &prd[(i - 2) * si + (j + 0) * sj];
                double *plc = &prd[(i - 1) * si + (j + 0) * sj];
                double *prc = &prd[(i + 1) * si + (j + 0) * sj];
                double *psc = &prd[(i + 2) * si + (j + 0) * sj];
                double *pck = &prd[(i + 0) * si + (j - 2) * sj];
                double *pcl = &prd[(i + 0) * si + (j - 1) * sj];
                double *pcr = &prd[(i + 0) * si + (j + 1) * sj];
                double *pcs = &prd[(i + 0) * si + (j + 2) * sj];

                for (int q = 0; q < NCONS; ++q)
                {
                    double gil = plm_minmod(pkc[q], plc[q], pcc[q], plm_theta);
                    double gic = plm_minmod(plc[q], pcc[q], prc[q], plm_theta);
                    double gir = plm_minmod(pcc[q], prc[q], psc[q], plm_theta);
                    double gjl = plm_minmod(pck[q], pcl[q], pcc[q], plm_theta);
                    double gjc = plm_minmod(pcl[q], pcc[q], pcr[q], plm_theta);
                    double gjr = plm_minmod(pcc[q], pcr[q], pcs[q], plm_theta);

                    pilp[q] = plc[q] + 0.5 * gil;
                    picm[q] = pcc[q] - 0.5 * gic;
                    picp[q] = pcc[q] + 0.5 * gic;
                    pirm[q] = prc[q] - 0.5 * gir;
                    pjlp[q] = pcl[q] + 0.5 * gjl;
                    pjcm[q] = pcc[q] - 0.5 * gjc;
                    pjcp[q] = pcc[q] + 0.5 * gjc;
                    pjrm[q] = pcr[q] - 0.5 * gjr;
                }
                #endif

                riemann_hlle(pilp, picm, fm, 1);
                riemann_hlle(picp, pirm, fp, 1);
                riemann_hlle(pjlp, pjcm, gm, 2);
                riemann_hlle(pjcp, pjrm, gp, 2);

                prim_to_cons(pcc, ucc);

                for (int q = 0; q < NCONS; ++q)
                {
                    ucc[q] -= (fp[q] - fm[q] + gp[q] - gm[q]) * dt / dx;
                    #if RUNGE_KUTTA == 1
                    ucc[q] *= (1.0 - rk);
                    ucc[q] += rk * urk[i * si + j * sj + q];
                    #endif
                }
                cons_to_prim(ucc, &pwr[i * si + j * sj]);
            }
        }
        """
        return prd.shape[:2], (
            prd,
            pwr,
            urk,
            dt,
            dx,
            rk,
            plm_theta,
            prd.shape[0],
            prd.shape[1],
        )

    @kernel
    def prim_to_cons_array(
        self,
        p: NDArray[float],
        u: NDArray[float],
        ni: int = None,
    ):
        R"""
        KERNEL void prim_to_cons_array(double *p, double *u, int ni)
        {
            FOR_EACH_1D(ni)
            {
                prim_to_cons(&p[NCONS * i], &u[NCONS * i]);
            }
        }
        """
        nq = p.shape[-1]
        return p.size // nq, (p, u, p.size // nq)


class SolverClass:
    @classmethod
    def get(cls, time_integration, reconstruction):
        if reconstruction == "pcm":
            if time_integration == "fwd":
                return cls.fwd_pcm
            elif time_integration in ("rk1", "rk2", "rk3"):
                return cls.rkn_pcm
        elif reconstruction == "plm":
            if time_integration == "fwd":
                return cls.fwd_plm
            elif time_integration in ("rk1", "rk2", "rk3"):
                return cls.rkn_plm
        else:
            raise ValueError(f"no solver config {time_integration}/{reconstruction}")


class FPFSolvers(SolverClass):
    @classmethod
    def init(cls):
        cls.fwd_pcm = FluxPerFaceSolver(plm=False, runge_kutta=False)
        cls.rkn_pcm = FluxPerFaceSolver(plm=False, runge_kutta=True)
        cls.fwd_plm = FluxPerFaceSolver(plm=True, runge_kutta=False)
        cls.rkn_plm = FluxPerFaceSolver(plm=True, runge_kutta=True)


class FPZSolvers(SolverClass):
    @classmethod
    def init(cls):
        cls.fwd_pcm = FluxPerZoneSolver(plm=False, runge_kutta=False)
        cls.rkn_pcm = FluxPerZoneSolver(plm=False, runge_kutta=True)
        cls.fwd_plm = FluxPerZoneSolver(plm=True, runge_kutta=False)
        cls.rkn_plm = FluxPerZoneSolver(plm=True, runge_kutta=True)


class FPZSolvers2D(SolverClass):
    @classmethod
    def init(cls):
        cls.fwd_pcm = FluxPerZoneSolver2D(plm=False, runge_kutta=False)
        cls.rkn_pcm = FluxPerZoneSolver2D(plm=False, runge_kutta=True)
        cls.fwd_plm = FluxPerZoneSolver2D(plm=True, runge_kutta=False)
        cls.rkn_plm = FluxPerZoneSolver2D(plm=True, runge_kutta=True)


def update_prim(
    p,
    dt,
    dx,
    fluxing="per_zone",
    reconstruction="pcm",
    plm_theta=2.0,
    time_integration="fwd",
    exec_mode="cpu",
):
    """
    Drives a first-order update of a primitive array
    """
    xp = numpy_or_cupy(exec_mode)

    if time_integration == "fwd":
        pass
    elif time_integration == "rk1":
        rks = [0.0]
    elif time_integration == "rk2":
        rks = [0.0, 0.5]
    elif time_integration == "rk3":
        rks = [0.0, 3.0 / 4.0, 1.0 / 3.0]
    else:
        raise ValueError(
            f"time_integration must be [fwd|r1k|rk2|rk3], got {time_integration}"
        )

    if fluxing == "per_face":
        solver = FPFSolvers.get(time_integration, reconstruction)
        update = solver.update_prim
        fluxes = solver.compute_godunov_fluxes
        prim_to_cons = solver.prim_to_cons_array
        f = xp.empty_like(p)

        if time_integration == "fwd":
            fluxes(p, f, plm_theta)
            update(p, f, None, dt, dx, 0.0)
        else:
            urk = xp.empty_like(p)
            prim_to_cons(p, urk)

            for rk in rks:
                fluxes(p, f, plm_theta)
                update(p, f, urk, dt, dx, rk)
        return p

    elif fluxing == "per_zone":
        solver = FPZSolvers.get(time_integration, reconstruction)
        update = solver.update_prim
        prim_to_cons = solver.prim_to_cons_array
        prd = p
        pwr = p.copy()

        if time_integration == "fwd":
            update(prd, pwr, None, dt, dx, 0.0, plm_theta)
            prd, pwr = pwr, prd

        else:
            urk = xp.empty_like(prd)
            prim_to_cons(prd, urk)

            for rk in rks:
                update(prd, pwr, urk, dt, dx, rk, plm_theta)
                prd, pwr = pwr, prd

        return prd
    else:
        raise ValueError(f"unknown fluxing {fluxing}")


def update_prim2d(
    p,
    dt,
    dx,
    fluxing="per_zone",
    reconstruction="pcm",
    plm_theta=2.0,
    time_integration="fwd",
    exec_mode="cpu",
):
    """
    Drives a first-order update of a primitive array
    """
    xp = numpy_or_cupy(exec_mode)

    if time_integration == "fwd":
        pass
    elif time_integration == "rk1":
        rks = [0.0]
    elif time_integration == "rk2":
        rks = [0.0, 0.5]
    elif time_integration == "rk3":
        rks = [0.0, 3.0 / 4.0, 1.0 / 3.0]
    else:
        raise ValueError(
            f"time_integration must be [fwd|r1k|rk2|rk3], got {time_integration}"
        )

    if fluxing != "per_zone":
        raise ValueError("only fluxing=per_zone supported in 2d")

    solver = FPZSolvers2D.get(time_integration, reconstruction)
    update = solver.update_prim
    prim_to_cons = solver.prim_to_cons_array

    prd = p
    pwr = p.copy()

    if time_integration == "fwd":
        update(prd, pwr, None, dt, dx, 0.0, plm_theta)
        prd, pwr = pwr, prd

    else:
        urk = xp.empty_like(prd)
        prim_to_cons(prd, urk)

        for rk in rks:
            update(prd, pwr, urk, dt, dx, rk, plm_theta)
            prd, pwr = pwr, prd

    return prd


def patch_spacing(index, nz, np):
    level, (i, j) = index
    dx = 1.0 / np / nz / (1 << level)
    dy = 1.0 / np / nz / (1 << level)
    return dx, dy


def patch_extent(index, nz, np):
    level, (i, j) = index
    dx = 1.0 / np / (1 << level)
    dy = 1.0 / np / (1 << level)
    x0 = -0.5 + (i + 0) * dx
    x1 = -0.5 + (i + 1) * dx
    y0 = -0.5 + (j + 0) * dy
    y1 = -0.5 + (j + 1) * dy
    return (x0, x1), (y0, y1)


def cell_centers_1d(ni):
    from numpy import linspace

    xv = linspace(0.0, 1.0, ni)
    xc = 0.5 * (xv[1:] + xv[:-1])
    return xc


def cell_centers_2d(index, nz, np):
    (x0, x1), (y0, y1) = patch_extent(index, nz, np)
    ni = nz
    nj = nz
    ddx = (x1 - x0) / ni
    ddy = (y1 - y0) / nj
    xv = linspace(x0 - 2 * ddx, x1 + 2 * ddy, ni + 5)
    yv = linspace(y0 - 2 * ddx, y1 + 2 * ddy, nj + 5)
    xc = 0.5 * (xv[1:] + xv[:-1])
    yc = 0.5 * (yv[1:] + yv[:-1])
    return meshgrid(xc, yc, indexing="ij")


def linear_shocktube(x):
    """
    A linear shocktube setup
    """

    from numpy import array, zeros, logical_not

    l = x < 0.5
    r = logical_not(l)
    p = zeros(x.shape + (3,))
    p[l, :] = [1.0, 0.0, 1.000]
    p[r, :] = [0.1, 0.0, 0.125]
    return p


def cylindrical_shocktube(x, y, radius: float = 0.1, pressure: float = 1.0):
    """
    A cylindrical shocktube setup

    ----------
    radius ........ radius of the high-pressure region
    pressure ...... gas pressure inside the cylinder
    """
    disk = (x**2 + y**2) ** 0.5 < radius
    fisk = logical_not(disk)
    p = zeros(disk.shape + (4,))

    p[disk, 0] = 1.000
    p[fisk, 0] = 0.100
    p[disk, 3] = pressure
    p[fisk, 3] = 0.125
    return p


def numpy_or_cupy(exec_mode):
    if exec_mode == "gpu":
        import cupy

        return cupy

    if exec_mode == "cpu":
        import numpy

        return numpy


class State:
    def __init__(self, n, t, p):
        self._n = n
        self._t = t
        self._p = p

    @property
    def iteration(self):
        return self._n

    @property
    def time(self):
        return self._t

    @property
    def primitive(self):
        try:
            return self._p.get()
        except AttributeError:
            return self._p

    @property
    def total_zones(self):
        return self._p.shape[0]


def simulation1d(exec_mode, resolution, fluxing, reconstruction, time_integration):
    from functools import partial

    xp = numpy_or_cupy(exec_mode)
    nz = resolution
    dx = 1.0 / nz
    dt = dx * 1e-1
    x = cell_centers_1d(nz)
    p = linear_shocktube(x)
    t = 0.0
    n = 0
    p = xp.array(p)
    iteration = 0

    advance = partial(
        update_prim,
        dx=dx,
        fluxing=fluxing,
        reconstruction=reconstruction,
        time_integration=time_integration,
        exec_mode=exec_mode,
    )
    yield State(n, t, p)

    while True:
        p = advance(p, dt)
        t += dt
        n += 1
        yield State(n, t, p)


class State2D:
    def __init__(self, n, t, p):
        self._n = n
        self._t = t
        self._p = p

    @property
    def iteration(self):
        return self._n

    @property
    def time(self):
        return self._t

    @property
    def primitive(self):
        try:
            return self._p.get()
        except AttributeError:
            return self._p

    @property
    def total_zones(self):
        return self._p.shape[0] * self._p.shape[1]


def simulation2d(exec_mode, resolution, fluxing, reconstruction, time_integration):
    from functools import partial

    xp = numpy_or_cupy(exec_mode)
    nz = resolution
    dx = 1.0 / nz
    dt = dx * 1e-1
    x, y = cell_centers_2d(index=(0, (0, 0)), nz=nz, np=1)
    p = cylindrical_shocktube(x, y)
    t = 0.0
    n = 0
    p = xp.array(p)
    iteration = 0

    advance = partial(
        update_prim2d,
        dx=dx,
        fluxing=fluxing,
        reconstruction=reconstruction,
        time_integration=time_integration,
        exec_mode=exec_mode,
    )
    yield State2D(n, t, p)

    while True:
        p = advance(p, dt)
        t += dt
        n += 1
        yield State2D(n, t, p)


@configurable
def driver(
    app_config,
    exec_mode: str = "cpu",
    resolution: int = 10000,
    tfinal: float = 0.1,
    fluxing: str = "per_zone",
    reconstruction: str = "pcm",
    plm_theta: float = 1.5,
    time_integration: str = "fwd",
    dim: int = 1,
    fold: int = 100,
    plot: bool = False,
):
    """
    Configuration
    -------------

    exec_mode:        execution mode [cpu|gpu]
    resolution:       number of grid zones
    tfinal:           time to end the simulation
    fluxing:          solver fluxing [per_zone|per_face]
    reconstruction:   first or second-order reconstruction [pcm|plm]
    plm_theta:        PLM parameter [1.0, 2.0]
    time_integration: Runge-Kutta order [fwd|rk1|rk2|rk3]
    dim:              dimensionality of the domain
    fold:             number of iterations between iteration message
    plot:             whether to show a plot of the solution
    """
    from reporting import terminal, iteration_msg

    configure_kernel_module(default_exec_mode=exec_mode)
    FPZSolvers.init()
    FPFSolvers.init()
    FPZSolvers2D.init()

    term = terminal(logger)
    simulation = [None, simulation1d, simulation2d][dim]
    sim = simulation(
        exec_mode,
        resolution,
        fluxing,
        reconstruction,
        time_integration,
    )
    perf_timer = perf_time_sequence(mode=exec_mode)
    state = next(sim)
    logger.info(f"initialization tool {1e3 * next(perf_timer):.3f}ms")
    logger.info("start simulation")

    while state.time < tfinal:
        state = next(sim)

        # if tasks.checkpoint.is_due(state.time):
        #     write_checkpoint(state, timeseries, tasks)

        # if tasks.timeseries.is_due(state.time):
        #     for name, info in diagnostics.items():
        #         timeseries[name].append(state.diagnostic(info))

        if state.iteration % fold == 0:
            zps = state.total_zones / next(perf_timer) * fold
            term(iteration_msg(state.iteration, state.time, zps=zps))

    if plot:
        from matplotlib import pyplot as plt

        if dim == 1:
            plt.plot(state.primitive[:, 0], "-o", mfc="none", label=fluxing)
        if dim == 2:
            plt.imshow(state.primitive[:, :, 0])
        plt.show()


def flatten_dict(
    d: MutableMapping,
    parent_key: str = "",
    sep: str = ".",
) -> MutableMapping:
    """
    Create a flattened dictionary e from d, with e['a.b.c'] = d['a']['b']['c'].
    """
    items = list()
    for k, v in d.items():
        new_key = parent_key + sep + k if parent_key else k
        if isinstance(v, MutableMapping):
            items.extend(flatten_dict(v, new_key, sep=sep).items())
        else:
            items.append((new_key, v))
    return dict(items)


def dict_section(d: MutableMapping, section: str):
    """
    From a map with keys like section.b.c, return a dict with keys like b.c.
    """
    return {k[k.index(".") + 1 :]: v for k, v in d.items() if k.startswith(section)}


def load_config(config):
    """
    Attempt to load configuration data from a file: either JSON or YAML.
    """
    if config.endswith(".json"):
        from json import load

        with open(config, "r") as infile:
            return load(infile)

    elif config.endswith(".yaml"):
        from yaml import load, CLoader

        with open(config, "r") as infile:
            return load(infile, Loader=CLoader)

    else:
        raise ValueError(f"unknown configuration file {config}")


def short_help(args):
    args.parser.print_usage()


def run(args):
    app_config = ChainMap(
        {k: v for k, v in vars(args).items() if v is not None and "." in k}
    )
    app_config.maps.extend(flatten_dict(load_config(c)) for c in reversed(args.configs))
    driver_args = dict_section(app_config, "driver")
    driver.schema.validate(**driver_args)
    driver.schema.print_schema(
        args.term,
        config=driver_args,
        newline=True,
    )
    driver(app_config, **driver_args)


def show_config(args):
    if args.defaults:
        for schema in all_schemas():
            schema.print_schema(args.term)

    else:
        app_cfg = {s.component_name: s.defaults_dict() for s in all_schemas()}

        if args.format == "json":
            from json import dumps

            print(dumps(app_cfg, indent=4))

        if args.format == "yaml":
            try:
                from yaml import dump, CDumper

                print(dump(app_cfg, Dumper=CDumper))

            except ImportError as e:
                print(e)


@logger.catch
def main():
    from argparse import ArgumentParser
    from reporting import add_logging_arguments, terminal, configure_logger

    parser = ArgumentParser()
    parser.set_defaults(func=short_help)
    parser.set_defaults(term=terminal(logger))
    parser.set_defaults(parser=parser)
    parser.set_defaults(log_level="info")
    subparsers = parser.add_subparsers()

    show_config_parser = subparsers.add_parser(
        "show-config",
        help="show global configuration data",
    )
    show_config_parser.set_defaults(func=show_config)
    group = show_config_parser.add_mutually_exclusive_group()
    group.add_argument(
        "--format",
        type=str,
        default="json",
        choices=["json", "yaml"],
        help="output format for the configuration data",
    )
    group.add_argument(
        "--defaults",
        action="store_true",
        help="print defaults and help messages for configurable components",
    )
    run_parser = subparsers.add_parser(
        "run",
        help="run a simulation",
    )
    run_parser.set_defaults(func=run)
    run_parser.add_argument(
        "configs",
        nargs="*",
        help="sequence of presets, configuration files, or checkpoints",
    )
    driver.schema.argument_parser(run_parser, dest_prefix="driver")
    add_logging_arguments(run_parser)

    args = parser.parse_args()
    configure_logger(logger, log_level=args.log_level)

    args.func(args)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print()
        logger.success("ctrl-c interrupt")
