"""Module for plasma (3d solver) initialization routines."""
import numpy as np
import cupy as cp

from ..config.config import Config
from .data import GPUArrays
from .weights_dual import initial_deposition

ELECTRON_CHARGE = -1
ELECTRON_MASS = 1


# Solving Laplace equation with Dirichlet boundary conditions (Ez) #

def dirichlet_matrix(grid_steps, grid_step_size):
    """
    Calculate a magical matrix that solves the Laplace equation
    if you elementwise-multiply the RHS by it "in DST-space".
    See Samarskiy-Nikolaev, p. 187.
    """
    # mul[i, j] = 1 / (lam[i] + lam[j])
    # lam[k] = 4 / h**2 * sin(k * pi * h / (2 * L))**2, where L = h * (N - 1)
    k = np.arange(1, grid_steps - 1)
    lam = 4 / grid_step_size**2 * np.sin(k * np.pi / (2 * (grid_steps - 1)))**2
    lambda_i, lambda_j = lam[:, None], lam[None, :]
    mul = 1 / (lambda_i + lambda_j)
    return mul / (2 * (grid_steps - 1))**2  # additional 2xDST normalization


# Solving Laplace or Helmholtz equation with mixed boundary conditions #

def mixed_matrix(grid_steps, grid_step_size, subtraction_trick):
    """
    Calculate a magical matrix that solves the Helmholtz or Laplace equation
    (subtraction_trick=True and subtraction_trick=False correspondingly)
    if you elementwise-multiply the RHS by it "in DST-DCT-transformed-space".
    See Samarskiy-Nikolaev, p. 189 and around.
    """
    # mul[i, j] = 1 / (lam[i] + lam[j])
    # lam[k] = 4 / h**2 * sin(k * pi * h / (2 * L))**2, where L = h * (N - 1)
    # but k for lam_i spans from 1..N-2, while k for lam_j covers 0..N-1
    ki, kj = np.arange(1, grid_steps - 1), np.arange(grid_steps)
    li = 4 / grid_step_size**2 * np.sin(ki * np.pi / (2 * (grid_steps - 1)))**2
    lj = 4 / grid_step_size**2 * np.sin(kj * np.pi / (2 * (grid_steps - 1)))**2
    lambda_i, lambda_j = li[:, None], lj[None, :]
    mul = 1 / (lambda_i + lambda_j + (1 if subtraction_trick else 0))
    return mul / (2 * (grid_steps - 1))**2  
    # return additional 2xDST normalization


# Solving Laplace equation with Neumann boundary conditions (Bz) #

def neumann_matrix(grid_steps, grid_step_size):
    """
    Calculate a magical matrix that solves the Laplace equation
    if you elementwise-multiply the RHS by it "in DST-space".
    See Samarskiy-Nikolaev, p. 187.
    """
    # mul[i, j] = 1 / (lam[i] + lam[j])
    # lam[k] = 4 / h**2 * sin(k * pi * h / (2 * L))**2, where L = h * (N - 1)

    k = np.arange(0, grid_steps)
    lam = 4 / grid_step_size**2 * np.sin(k * np.pi / (2 * (grid_steps - 1)))**2
    lambda_i, lambda_j = lam[:, None], lam[None, :]
    mul = 1 / (lambda_i + lambda_j)  # WARNING: zero division in mul[0, 0]!
    mul[0, 0] = 0  # doesn't matter anyway, just defines constant shift
    return mul / (2 * (grid_steps - 1))**2  # additional 2xDST normalization


# Coarse and fine plasma particles initialization #

def make_coarse_plasma_grid(steps, step_size, coarseness):
    """
    Create initial coarse plasma particles coordinates
    (a single 1D grid for both x and y).
    """
    assert coarseness == int(coarseness)  # TODO: why?
    plasma_step = step_size * coarseness
    right_half = np.arange(steps // (coarseness * 2)) * plasma_step
    left_half = -right_half[:0:-1]  # invert, reverse, drop zero
    plasma_grid = np.concatenate([left_half, right_half])
    assert(np.array_equal(plasma_grid, -plasma_grid[::-1]))
    return plasma_grid


def make_fine_plasma_grid(steps, step_size, fineness):
    """
    Create initial fine plasma particles coordinates
    (a single 1D grid for both x and y).
    Avoids positioning particles at the cell edges and boundaries, example:
    `fineness=3` (and `coarseness=2`):
        +-----------+-----------+-----------+-----------+
        | .   .   . | .   .   . | .   .   . | .   .   . |
        |           |           |           |           |   . - fine particle
        | .   .   . | .   *   . | .   .   . | .   *   . |
        |           |           |           |           |   * - coarse particle
        | .   .   . | .   .   . | .   .   . | .   .   . |
        +-----------+-----------+-----------+-----------+
    `fineness=2` (and `coarseness=2`):
        +-------+-------+-------+-------+-------+
        | .   . | .   . | .   . | .   . | .   . |           . - fine particle
        |       |   *   |       |   *   |       |
        | .   . | .   . | .   . | .   . | .   . |           * - coarse particle
        +-------+-------+-------+-------+-------+
    """
    assert fineness == int(fineness)
    plasma_step = step_size / fineness
    if fineness % 2:  # some on zero axes, none on cell corners
        right_half = np.arange(steps // 2 * fineness) * plasma_step
        left_half = -right_half[:0:-1]  # invert, reverse, drop zero
    else:  # none on zero axes, none on cell corners
        right_half = (.5 + np.arange(steps // 2 * fineness)) * plasma_step
        left_half = -right_half[::-1]  # invert, reverse
    plasma_grid = np.concatenate([left_half, right_half])
    assert(np.array_equal(plasma_grid, -plasma_grid[::-1]))
    return plasma_grid


def make_plasma(steps, cell_size, coarseness=2, fineness=2):
    """
    Make coarse plasma initial state arrays and the arrays needed to intepolate
    coarse plasma into fine plasma (`virt_params`).
    Coarse is the one that will evolve and fine is the one to be bilinearly
    interpolated from the coarse one based on the initial positions
    (using 1 to 4 coarse plasma particles that initially were the closest).
    """
    coarse_step = cell_size * coarseness

    # Make two initial grids of plasma particles, coarse and fine.
    # Coarse is the one that will evolve and fine is the one to be bilinearly
    # interpolated from the coarse one based on the initial positions.

    coarse_grid = make_coarse_plasma_grid(steps, cell_size, coarseness)
    coarse_grid_xs, coarse_grid_ys = coarse_grid[:, None], coarse_grid[None, :]

    fine_grid = make_fine_plasma_grid(steps, cell_size, fineness)

    Nc = len(coarse_grid)

    # Create plasma electrons on the coarse grid, the ones that really move
    coarse_x_init = cp.broadcast_to(cp.asarray(coarse_grid_xs), (Nc, Nc))
    coarse_y_init = cp.broadcast_to(cp.asarray(coarse_grid_ys), (Nc, Nc))
    coarse_x_offt = cp.zeros((Nc, Nc))
    coarse_y_offt = cp.zeros((Nc, Nc))
    coarse_px = cp.zeros((Nc, Nc))
    coarse_py = cp.zeros((Nc, Nc))
    coarse_pz = cp.zeros((Nc, Nc))
    coarse_m = cp.ones((Nc, Nc)) * ELECTRON_MASS * coarseness**2
    coarse_q = cp.ones((Nc, Nc)) * ELECTRON_CHARGE * coarseness**2

    # Calculate indices for coarse -> fine bilinear interpolation

    # Neighbour indices array, 1D, same in both x and y direction.
    indices = np.searchsorted(coarse_grid, fine_grid)
    # example:
    #     coarse:  [-2., -1.,  0.,  1.,  2.]
    #     fine:    [-2.4, -1.8, -1.2, -0.6,  0. ,  0.6,  1.2,  1.8,  2.4]
    #     indices: [ 0  ,  1  ,  1  ,  2  ,  2  ,  3  ,  4  ,  4  ,  5 ]
    # There is no coarse particle with index 5, so clip it to 4:
    indices_next = np.clip(indices, 0, Nc - 1)  # [0, 1, 1, 2, 2, 3, 4, 4, 4]
    # Clip to zero for indices of prev particles as well:
    indices_prev = np.clip(indices - 1, 0, Nc - 1)  # [0, 0, 0, 1 ... 3, 3, 4]
    # mixed from: [ 0&0 , 0&1 , 0&1 , 1&2 , 1&2 , 2&3 , 3&4 , 3&4, 4&4 ]

    # Calculate weights for coarse->fine interpolation from initial positions.
    # The further the fine particle is from closest right coarse particles,
    # the more influence the left ones have.
    influence_prev = (coarse_grid[indices_next] - fine_grid) / coarse_step
    influence_next = (fine_grid - coarse_grid[indices_prev]) / coarse_step
    # Fix for boundary cases of missing cornering particles.
    influence_prev[indices_next == 0] = 0   # nothing on the left?
    influence_next[indices_next == 0] = 1   # use right
    influence_next[indices_prev == Nc - 1] = 0  # nothing on the right?
    influence_prev[indices_prev == Nc - 1] = 1  # use left
    # Same arrays are used for interpolating in y-direction.

    # The virtualization formula is thus
    # influence_prev[pi] * influence_prev[pj] * <bottom-left neighbour value> +
    # influence_prev[pi] * influence_next[nj] * <top-left neighbour value> +
    # influence_next[ni] * influence_prev[pj] * <bottom-right neighbour val> +
    # influence_next[ni] * influence_next[nj] * <top-right neighbour value>
    # where pi, pj are indices_prev[i], indices_prev[j],
    #       ni, nj are indices_next[i], indices_next[j] and
    #       i, j are indices of fine virtual particles

    # This is what is employed inside mix() and deposit_kernel().

    # An equivalent formula would be
    # inf_prev[pi] * (inf_prev[pj] * <bot-left> + inf_next[nj] * <bot-right>) +
    # inf_next[ni] * (inf_prev[pj] * <top-left> + inf_next[nj] * <top-right>)

    # Values of m, q, px, py, pz should be scaled by 1/(fineness*coarseness)**2

    virt_params = GPUArrays(
        influence_prev=influence_prev, influence_next=influence_next,
        indices_prev=indices_prev, indices_next=indices_next,
        fine_grid=fine_grid,
    )

    return (
        coarse_x_init, coarse_y_init, coarse_x_offt, coarse_y_offt,
        coarse_px, coarse_py, coarse_pz, coarse_m, coarse_q, virt_params
    )


def init_plasma(config: Config):
    """
    Initialize all the arrays needed (for what?).
    """
    grid_steps            = config.getint('window-width-steps')
    grid_step_size        = config.getfloat('window-width-step-size')
    reflect_padding_steps = config.getint('reflect-padding-steps')
    plasma_padding_steps  = config.getint('plasma-padding-steps')
    plasma_coarseness     = config.getint('plasma-coarseness')
    plasma_fineness       = config.getint('plasma-fineness')
    solver_trick          = config.getint('field-solver-subtraction-trick')

    # for convenient diagnostics, a cell should be in the center of the grid
    assert grid_steps % 2 == 1

    # virtual particles should not reach the window pre-boundary cells
    assert reflect_padding_steps > plasma_coarseness + 1
    # TODO: The (costly) alternative is to reflect after plasma virtualization,
    #       but it's better for stabitily, or is it?

    # particles should not reach the window pre-boundary cells
    if reflect_padding_steps <= 2:
        raise Exception("'reflect_padding_steps' parameter is too low.\n" +
                        "Details: 'reflect_padding_steps' must be bigger than" +
                        " 2. By default it is 5.")

    x_init, y_init, x_offt, y_offt, px, py, pz, q, m, virt_params = \
        make_plasma(
            grid_steps - plasma_padding_steps * 2, grid_step_size,
            coarseness=plasma_coarseness, fineness=plasma_fineness
        )

    ro_initial = initial_deposition(
        grid_steps, grid_step_size, plasma_coarseness, plasma_fineness,
        x_offt, y_offt, px, py, pz, m, q, virt_params
    )
    dir_matrix = dirichlet_matrix(grid_steps, grid_step_size)
    mix_matrix = mixed_matrix(grid_steps, grid_step_size, solver_trick)
    neu_matrix = neumann_matrix(grid_steps, grid_step_size)

    def zeros():
        return cp.zeros((grid_steps, grid_steps), dtype=cp.float64)

    fields = GPUArrays(Ex=zeros(), Ey=zeros(), Ez=zeros(),
                       Bx=zeros(), By=zeros(), Bz=zeros(),
                       Phi=zeros())

    particles = GPUArrays(x_init=x_init, y_init=y_init,
                          x_offt=x_offt, y_offt=y_offt,
                          px=px, py=py, pz=pz, q=q, m=m)

    currents = GPUArrays(ro=zeros(), jx=zeros(), jy=zeros(), jz=zeros())

    const_arrays = GPUArrays(
        ro_initial=ro_initial, dirichlet_matrix=dir_matrix,
        field_mixed_matrix=mix_matrix, neumann_matrix=neu_matrix,
        influence_prev=virt_params.influence_prev,
        influence_next=virt_params.influence_next,
        indices_prev=virt_params.indices_prev,
        indices_next=virt_params.indices_next,
        fine_grid=virt_params.fine_grid
    )

    return fields, particles, currents, const_arrays


def load_plasma(config: Config, path_to_plasmastate: str):
    _, _, _, const_arrays = init_plasma(config)

    with np.load(file=path_to_plasmastate) as state:
        fields = GPUArrays(Ex=state['Ex'], Ey=state['Ey'],
                           Ez=state['Ez'], Bx=state['Bx'],
                           By=state['By'], Bz=state['Bz'],
                           Phi=state['Phi'])

        particles = GPUArrays(x_init=particles.x_init, y_init=particles.y_init,
                              q=particles.q, m=particles.m,
                              x_offt=state['x_offt'], y_offt=state['y_offt'],
                              px=state['px'], py=state['py'], pz=state['pz'])

        currents = GPUArrays(ro=state['ro'], jx=state['jx'],
                             jy=state['jy'], jz=state['jz'])

    return fields, particles, currents, const_arrays
