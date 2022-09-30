"""Top-level three-dimensional simulation class."""
# General imports
import numpy as np

# Config
from ..config.default_config_values import default_config_values
from ..config.config import Config

# Diagnostics
from ..diagnostics.diagnostics_3d import Diagnostics3d

# Imports for beam generating in 3d (can be used for 2d also)
from ..alt_beam_generator.beam_generator import generate_beam
from ..alt_beam_generator.beam_generator import particle_dtype3d
from ..alt_beam_generator.beam_shape import BeamShape, BeamSegmentShape

# Imports for 3d simulation
from ..push_solvers.push_solver_3d import PushAndSolver3d as PushAndSolver3dCPU
from .. import beam3d as beam3d_cpu_module
from ..plasma3d.initialization import init_plasma as init_plasma_cpu
from ..plasma3d.initialization import load_plasma as load_plasma_cpu

from ..push_solvers.push_solver_3d_gpu import PushAndSolver3d as PushAndSolver3dGPU
from .. import beam3d_gpu as beam3d_gpu_module

# Single plasma approach
from ..plasma3d_gpu.initialization import init_plasma as init_plasma_gpu_single
from ..plasma3d_gpu.initialization import load_plasma as load_plasma_gpu_single

# Dual plasma approach
from ..plasma3d_gpu.initialization_dual import init_plasma as init_plasma_gpu_dual
from ..plasma3d_gpu.initialization_dual import load_plasma as load_plasma_gpu_dual


class Cartesian3dSimulation:
    """
    Top-level lcodePy simulation class for cartesian 3d geometry.

    This class contains configuration of simulation and controls diagnostics.
    """
    def __init__(self, config=default_config_values, beam_parameters={},
                 diagnostics=None):
        self.config = config
        self.beam_parameters = beam_parameters
        self.diagnostics = diagnostics

        # Here we set parameters for beam generation, where we will store beam
        # particles and where they will go after calculations
        # self.beam_parameters = beam_parameters
        # self.beam_particle_dtype = particle_dtype3d

        # We use this time as a general time value:
        self.current_time = 0.
        # The user can change this value for diagnostic convenience.

        # We initialize a beam source and a beam drain:
        self.beam_source = None
        self.beam_drain = None

        # We set that initially the code doesn't use an external plasma state:
        self.external_plasmastate = False
        self.path_to_plasmastate = 'plasmastate.npz'

        # Pull the config before the start of calculations:
        self.__pull_config()

    def __pull_config(self):
        # 0. We set __config__ as a Config class instance:
        self.__config = Config(self.config)
        self.full_config = self.__config.config_values

        # Firstly, we check that the geomtry was set right:
        geometry = self.__config.get('geometry').lower()
        if geometry != '3d':
            raise Exception("Sorry, you set a wrong type of geometry. If you" +
                            "want to use Cartesian3dSimulation, change" +
                            f"geometry from {geometry} to 3d in your config." +
                            "(your_config['geometry'] = '3d')")

        # We set some instance variables:
        self.__time_limit = self.__config.getfloat('time-limit')
        self.__time_step_size = self.__config.getfloat('time-step')
        self.__rigid_beam = self.__config.get('rigid-beam')

        # Mode of plasma continuation:
        self.__cont_mode = self.__config.get('continuation')

        # Here we get information about the type of processing unit (CPU or GPU)
        pu_type = self.__config.get('processing-unit-type').lower()

        # Here we get information about signle or dual plasma approach
        dual_plasma_approach = self.__config.getbool('dual-plasma-approach')

        if pu_type == 'cpu':
            self.__beam_module = beam3d_cpu_module
            self.__push_solver = PushAndSolver3dCPU(self.__config)
            self.__init_plasma = init_plasma_cpu
            self.__load_plasma = load_plasma_cpu
        elif pu_type == 'gpu':
            self.__beam_module = beam3d_gpu_module
            self.__push_solver = PushAndSolver3dGPU(self.__config)
            if dual_plasma_approach == False:
                self.__init_plasma = init_plasma_gpu_single
                self.__load_plasma = load_plasma_gpu_single
            else:
                self.__init_plasma = init_plasma_gpu_dual
                self.__load_plasma = load_plasma_gpu_dual

        # Finally, we set the diagnostics.
        if type(self.diagnostics) != list and self.diagnostics is not None:
            # If a user set only one diag. class:
            self.diagnostics = [self.diagnostics]
        self.__diagnostics = Diagnostics3d(config=self.__config,
                                           diag_list=self.diagnostics)

    def load_beamfile(self, path_to_beamfile='beamfile.npz'):
        beam_particles = self.__beam_module.BeamParticles()
        beam_particles.load(path_to_beamfile)

        self.beam_source = self.__beam_module.BeamSource(self.__config,
                                                       beam_particles)
        self.beam_drain  = self.__beam_module.BeamDrain()

    # def add_beamfile(self, path_to_beamfile='new_beamfile.npz'):
    #     """Add a new beam that is loaded from 'path_to_beamfile' to the beam source.
    #     """
    #     pass

    def __load_plasmastate(self):
        (self.__loaded_fields, self.__loaded_particles,
        self.__loaded_currents, self.__const_arrays) =\
            self.__load_plasma(self.__config, self.path_to_plasmastate)

    def __init_plasmastate(self):
        # In case of an external plasma state, we set values
        # as the loaded values:
        if self.external_plasmastate:
            pl_fields, pl_particles, pl_currents, pl_const_arrays = (
                self.__loaded_fields, self.__loaded_particles,
                self.__loaded_currents, self.__const_arrays
            )
        else:
            # Initializes a plasma state:
            pl_fields, pl_particles, pl_currents, pl_const_arrays =\
                self.__init_plasma(self.__config)

        return pl_fields, pl_particles, pl_currents, pl_const_arrays

    def step(self, N_steps=None):
        """Compute N time steps."""
        # 0. It analyzes config values:
        self.__pull_config()

        # 1. If we use an external plasma state, we load it:
        if self.external_plasmastate:
            self.__load_plasmastate()

        # t step function, makes N_steps time steps.
        if N_steps is None:
            N_steps = int(self.__time_limit / self.__time_step_size)
            print("Since the number of time steps hasn't been set explicitly,",
                  f"the code will simulate {N_steps} time steps with a time",
                  f"step size = {self.__time_step_size}.")
        else:
            self.__time_limit = \
                N_steps * self.__time_step_size + self.current_time
            print("Since the number of time steps has been set explicitly,",
                  f"the code will simulate till time limit = {self.__time_limit},",
                  f"with a time step size = {self.__time_step_size}.")

        # 2. Checks for plasma continuation mode:
        if self.__cont_mode == 'n' or self.__cont_mode == 'no':
            # 3. If a beam source is empty (None), we generate
            #    a new beam according to set parameters:
            if self.beam_source is None:
                # Check for a beam being not rigid.
                if self.__rigid_beam == 'n' or self.__rigid_beam == 'no':
                    # Generate all parameters for a beam:
                    beam_particles = generate_beam(self.__config,
                                                   self.beam_parameters,
                                                   self.__beam_module)

                    # Here we create a beam source and a beam drain:
                    self.beam_source = self.__beam_module.BeamSource(
                                                self.__config, beam_particles)
                    self.beam_drain  = self.__beam_module.BeamDrain()

                # A rigid beam mode has not been implemented yet. If you are
                # writing rigid beam mode, just use rigid_beam_current(...) from
                # ..alt_beam_generator.beam_generator
                else:
                    raise Exception("Sorry, for now, only 'no' mode of" +
                                    "rigid-beam is supported.")

            # 4. A loop that calculates N time steps:
            for t_i in range(N_steps):
                # TODO: Check if it is correct!
                self.current_time = \
                    self.current_time + self.__time_step_size

                pl_fields, pl_particles, pl_currents, pl_const_arrays = \
                    self.__init_plasmastate()

                # Calculates one time step:
                self.__push_solver.step_dt(
                    pl_fields, pl_particles, pl_currents, pl_const_arrays,
                    self.beam_source, self.beam_drain, self.current_time,
                    self.__diagnostics
                )

                # Perform diagnostics
                self.__diagnostics.after_step_dt(
                    self.current_time, pl_particles, pl_fields, pl_currents,
                    self.beam_drain
                )

                # Here we transfer beam particles from beam_buffer to
                # beam_source for the next time step. And create a new beam
                # drain that is empty.
                self.beam_source = self.__beam_module.BeamSource(
                    self.__config, self.beam_drain.beam_buffer
                )
                self.beam_drain  = self.__beam_module.BeamDrain()

            # 4. As in lcode2d, we save the beam state on reaching the time limit:
            self.beam_source.beam.save('beamfile') # Do we need it?
            print('The work is done!')

        # Other plasma continuation mode has not been implemented yet.
        # If you are writing these modes, just change where you put
        # init_plasma(...) and generate_beam(...)
        else:
            raise Exception("Sorry, for now, only 'no' mode of plasma" +
                            "continuation is supported.")
