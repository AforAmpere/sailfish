#!/usr/bin/env python3

import argparse
import pickle
import sys

sys.path.insert(1, ".")


def plot_srhd_1d(ax, chkpt):
    mesh = chkpt["mesh"]
    x = mesh.zone_centers(chkpt["time"])
    rho = chkpt["primitive"][:, 0]
    vel = chkpt["primitive"][:, 1]
    pre = chkpt["primitive"][:, 2]
    ax.plot(x, rho, label=r"$\rho$")
    # ax.plot(x, vel, label=r"$\Gamma \beta$")
    # ax.plot(x, pre, label=r"$p$")

    if type(mesh) == sailfish.mesh.LogSphericalMesh:
        ax.set_xscale("log")
        ax.set_yscale("log")


def plot_srhd_2d(ax, chkpt):
    import numpy as np

    mesh = chkpt["mesh"]
    r = np.array(mesh.radial_vertices)
    q = np.array(mesh.polar_vertices)

    r, q = np.meshgrid(r, q)
    z = r * np.cos(q)
    x = r * np.sin(q)
    s = chkpt["primitive"][:, :, 0].T
    ax.set_aspect("equal")
    cm = ax.pcolormesh(x, z, s, edgecolors="none")
    return cm


def main(args):
    import matplotlib.pyplot as plt
    import sailfish

    fig, ax1 = plt.subplots()

    for filename in args.checkpoints:

        with open(filename, "rb") as f:
            chkpt = pickle.load(f)

        if chkpt["solver"] == "srhd_1d":
            plot_srhd_1d(ax1, chkpt)
            ax1.legend()

        elif chkpt["solver"] == "srhd_2d":
            cm = plot_srhd_2d(ax1, chkpt)
            fig.colorbar(cm)

    plt.show()


if __name__ == "__main__":
    args = argparse.ArgumentParser()
    args.add_argument("checkpoints", type=str, nargs="+")
    main(args.parse_args())
