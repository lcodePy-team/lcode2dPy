from math import inf, ceil
from pathlib import Path
import numpy as np
import matplotlib.pyplot as plt

from ..config.config import Config
from ..config.default_config import default_config

from ..plasma3d_gpu.data import GPUArraysView

# Auxiliary functions:

def from_str_into_list(names_str: str):
    """ Makes a list of elements that it gets from a long string."""
    # For example: 'Ez, Ey, Ez ,Bx,,' becomes ['Ez', 'Ey', 'Ez', 'Bx']
    names = np.array(names_str.replace(' ', '').split(','))
    names = names[names != '']
    return names


def conv_2d(arr: np.ndarray, merging_xi: int, merging_r: int):
    """Calculates strided convolution using a mean/uniform kernel."""
    new_arr = []
    for i in range(0, arr.shape[0], merging_xi):
        start_i, end_i = i, i + merging_xi
        if end_i > arr.shape[0]:
            end_i = arr.shape[0]

        for j in range(0, arr.shape[1], merging_r):
            start_j, end_j = j, j + merging_r
            if end_j > arr.shape[1]:
                end_j = arr.shape[1]

            new_arr.append(np.mean(arr[start_i:end_i, start_j:end_j]))

    return np.reshape(np.array(new_arr),
                      ceil(arr.shape[0] / merging_xi, -1))


# Diagnostics classes:

class Diagnostics3d:
    def __init__(self, config: Config, diag_list=None):
        """The main class of 3d diagnostics."""
        self.config = config
        if diag_list is None:
            self.diag_list = []
        else:
            self.diag_list = diag_list

        # Pushes a config to all choosen diagnostics classes:
        for diag in self.diag_list:
            try:
                diag.pull_config(self.config)
            except AttributeError:
                print(
                    f'{diag} type of diagnostics is not supported because' + 
                    'it does not have attribute called pull_config(...) or' +
                    'this attribute does not work correctly.'
                )

    def after_step_dxi(self, *parameters):
        for diag in self.diag_list:
            try:
                diag.after_step_dxi(*parameters)
            except AttributeError:
                print(
                    f'{diag} type of diagnostics is not supported because' + 
                    'it does not have attribute called after_step_dxi(...).' +
                    'this attribute does not work correctly.'
                )

    def dump(self, *parameters):
        for diag in self.diag_list:
            diag.dump(*parameters)

    def after_step_dt(self, *parameters):
        for diag in self.diag_list:
            try:
                diag.after_step_dt(*parameters)
            except AttributeError:
                print(
                    f'{diag} type of diagnostics is not supported because' + 
                    'it does not have attribute called after_step_dt(...).' +
                    'this attribute does not work correctly.'
                )


class DiagnosticsFXi:
    __allowed_f_xi = ['Ex', 'Ey', 'Ez', 'Bx', 'By', 'Bz', 'rho', 'rho_beam',
                      'Ez2', 'Bz2', 'rho_beam2', 'Phi']
                    # 'ni']
                    # TODO: add them and functionality!
    __allowed_f_xi_type = ['numbers', 'pictures', 'both']
    #TODO: add 'pictures' and 'both' and functionality

    def __init__(
        self, output_period=100, saving_xi_period=100, f_xi='Ez',
        f_xi_type='numbers', axis_x=0, axis_y=0, auxiliary_x=0, auxiliary_y=1
    ):
        # It creates a list of string elements such as Ez, Ex, By...
        self.__f_xi_names = from_str_into_list(f_xi)
        for name in self.__f_xi_names:
            if name not in self.__allowed_f_xi:
                raise Exception(f'{name} value is not supported as f(xi).')

        # Output mode for the functions of xi:
        if f_xi_type in self.__allowed_f_xi_type:
            self.__f_xi_type = f_xi_type
        else:
            raise Exception(f"{f_xi_type} type of f(xi) diagnostics is " +
                             "not supported.")

        # The position of a `probe' line 1 from the center:
        self.__axis_x, self.__axis_y = axis_x, axis_y
        # THe position of a `probe' line 2 from the center:
        self.__auxiliary_x, self.__auxiliary_y = auxiliary_x, auxiliary_y

        # Set time periodicity of detailed output and safving into a file:
        self.__output_period = output_period
        self.__saving_xi_period = saving_xi_period

        # We store data as a simple Python dictionary of lists for f(xi) data.
        # But I'm not sure this is the best way to handle data storing!
        self.__data = {name: [] for name in self.__f_xi_names}
        self.__data['xi'] = []

    def __repr__(self):
        return (
            f"DiagnosticsFXi(output_period={self.__output_period}, " +
            f"saving_xi_period={self.__saving_xi_period}, " + 
            f"f_xi='{','.join(self.__f_xi_names)}', " +
            f"f_xi_type='{self.__f_xi_type}', " +
            f"axis_x={self.__axis_x}, axis_y={self.__axis_y}, " +
            f"auxiliary_x={self.__auxiliary_x}, auxiliary_y={self.__auxiliary_y})"
        )

    def pull_config(self, config: Config):
        """Pulls a config to get all required parameters."""
        steps = config.getint('window-width-steps')
        step_size = config.getfloat('window-width-step-size')

        # We change how we store the positions of the 'probe' lines:
        self.__ax_x  = steps // 2 + int(self.__axis_x / step_size)
        self.__ax_y  = steps // 2 + int(self.__axis_y / step_size)
        self.__aux_x = steps // 2 + int(self.__auxiliary_x / step_size)
        self.__aux_y = steps // 2 + int(self.__auxiliary_y / step_size)

        # Here we check if the output period is less than the time step size.
        # In that case each time step is diagnosed. The first time step is
        # always diagnosed. And we check if output_period % time_step_size = 0,
        # because otherwise it won't diagnosed anything.
        self.__time_step_size = config.getfloat('time-step')
        self.__xi_step_size   = config.getfloat('xi-step')

        if self.__output_period < self.__time_step_size:
            self.__output_period = self.__time_step_size

        if self.__saving_xi_period < self.__xi_step_size:
            self.__saving_xi_period = self.__xi_step_size

        if self.__output_period % self.__time_step_size != 0:
            raise Exception(
                "The diagnostics will not work because " +
                f"{self.__output_period} % {self.__time_step_size} != 0"
            )

    def after_step_dxi(
        self, current_time, xi_plasma_layer, pl_particles, pl_fields,
        pl_currents, ro_beam
    ):
        if self.conditions_check(current_time, xi_plasma_layer):
            self.__data['xi'].append(xi_plasma_layer)

            for name in self.__f_xi_names:
                if name in ['Ex', 'Ey', 'Ez', 'Bx', 'By', 'Bz', 'Phi']:
                    val = getattr(pl_fields, name)[self.__ax_x, self.__ax_y]
                    self.__data[name].append(val)

                if name == 'rho':
                    # TODO: It's just a crutch!!!
                    val = getattr(pl_currents, 'ro')[self.__ax_x, self.__ax_y]
                    self.__data[name].append(val)

                if name == 'rho_beam':
                    val = ro_beam[self.__ax_x, self.__ax_y]
                    self.__data[name].append(val)

                if name in ['Ez2', 'Bz2']:
                    val = getattr(
                        pl_fields, name[:2])[self.__aux_x, self.__aux_y]
                    self.__data[name].append(val)

                if name == 'rho_beam2':
                    val = ro_beam[self.__aux_x, self.__aux_y]
                    self.__data[name].append(val)
        
        # We use dump here to save data not only at the end of the simulation
        # window, but with some period too.
        # TODO: Do we really need this? Does it work right?
        if xi_plasma_layer % self.__saving_xi_period <= self.__xi_step_size / 2:
            self.dump(current_time)

    def conditions_check(self, current_time, xi_pl_layer):
        return current_time % self.__output_period == 0

    def dump(self, current_time):
        if self.conditions_check(current_time, inf):
            Path('./diagnostics').mkdir(parents=True, exist_ok=True)
            if 'numbers' in self.__f_xi_type or 'both' in self.__f_xi_type:
                np.savez(f'./diagnostics/f_xi_{current_time:08.2f}.npz',
                            **self.__data)

            if 'pictures' in self.__f_xi_type or 'both' in self.__f_xi_type:
                for name in self.__f_xi_names:
                    plt.plot(self.__data['xi'], self.__data[name])
                    plt.savefig(
                        f'./diagnostics/{name}_f_xi_{current_time:08.2f}.png'
                    )
                                # vmin=-1, vmax=1)
                    plt.close()

    def after_step_dt(self, *params):
        # We use this function to clean old data:
        for name in self.__data:
            self.__data[name] = []


class DiagnosticsColormaps:
    # By default, colormaps are taken in (y, xi) plane.
    # TODO: Make (x, xi) plane an option.
    __allowed_colormaps = ['Ex', 'Ey', 'Ez', 'Bx', 'By', 'Bz', 'rho', 'rho_beam',
                           'px', 'py', 'pz', 'Phi']
                        # 'ni']
                    # TODO: add them and functionality!
    __allowed_colormaps_type = ['numbers']
    #TODO: add 'pictures' and 'both' and functionality

    def __init__(
        self, output_period=100, saving_xi_period=1000, colormaps='Ez',
        colormaps_type='numbers', xi_from=inf, xi_to=-inf, r_from=-inf, r_to=inf,
        output_merging_r: int=1, output_merging_xi: int=1
    ):
        # It creates a list of string elements such as Ez, Ex, By...
        self.__colormaps_names = from_str_into_list(colormaps)
        for name in self.__colormaps_names:
            if name not in self.__allowed_colormaps:
                raise Exception(f'{name} value is not supported as a colormap.')

        # Output mode for the functions of xi:
        if colormaps_type in self.__allowed_colormaps_type:
            self.__colormaps_type = colormaps_type
        else:
            raise Exception(f"{colormaps_type} type of colormap diagnostics" +
                             "is not supported.")

        # Set time periodicity of detailed output:
        self.__output_period = output_period
        self.__saving_xi_period = saving_xi_period

        # Set borders of a subwindow:
        self.__xi_from, self.__xi_to = xi_from, xi_to
        self.__r_from, self.__r_to = r_from, r_to

        # Set values for merging functionality:
        self.__merging_r  = int(output_merging_r)
        self.__merging_xi = int(output_merging_xi)

        # We store data as a Python dictionary of numpy arrays for colormaps
        # data. I'm not sure this is the best way to handle data storing.
        self.__data = {name: [] for name in self.__colormaps_names}
        self.__data['xi'] = []

    def __repr__(self) -> str:
        return (
            f"DiagnosticsColormaps(output_period={self.__output_period}, " +
            # f"saving_xi_period={self.__saving_xi_period}, " + 
            f"colormaps='{','.join(self.__colormaps_names)}', " +
            f"colormaps_type='{self.__colormaps_type}', " + 
            f"xi_from={self.__xi_from}, xi_to={self.__xi_to}, " + 
            f"r_from={self.__r_from}, r_to={self.__r_to}, " +
            f"output_merging_r={self.__merging_r}, " +
            f"output_merging_xi={self.__merging_xi})"
        )

    def pull_config(self, config: Config):
        """Pulls a config to get all required parameters."""
        self.__steps = config.getint('window-width-steps')
        step_size = config.getfloat('window-width-step-size')

        # Here we define subwindow borders in terms of number of steps:
        self.__r_f = self.__steps // 2 + self.__r_from / step_size
        self.__r_t = self.__steps // 2 + self.__r_to   / step_size

        # We check that r_f and r_t are in the borders of window.
        # Otherwise, we make them equal to the size of the window.
        if self.__r_t > self.__steps:
            self.__r_t = self.__steps
        else:
            self.__r_t = int(self.__r_t)

        if self.__r_f < 0:
            self.__r_f = 0
        else:
            self.__r_f = int(self.__r_f)

        # Here we check if the output period is less than the time step size.
        # In that case each time step is diagnosed. The first time step is
        # always diagnosed. And we check if period % time_step_size = 0,
        # because otherwise it won't diagnosed anything.
        self.__time_step_size = config.getfloat('time-step')
        self.__xi_step_size   = config.getfloat('xi-step')

        if self.__output_period < self.__time_step_size:
            self.__output_period = self.__time_step_size

        if self.__output_period % self.__time_step_size != 0:
            raise Exception(
                "The diagnostics will not work because " +
                f"{self.__output_period} % {self.__time_step_size} != 0"
            )

    def after_step_dxi(
        self, current_time, xi_plasma_layer, pl_particles, pl_fields,
        pl_currents, ro_beam
    ):
        if self.conditions_check(current_time, xi_plasma_layer):
            # Firstly, it adds the current value of xi to data dictionary:
            self.__data['xi'].append(xi_plasma_layer)

            for name in self.__colormaps_names:
                if name in ['Ex', 'Ey', 'Ez', 'Bx', 'By', 'Bz', 'Phi']:
                    val = getattr(pl_fields, name)[
                        self.__steps//2, self.__r_f:self.__r_t]
                    self.__data[name].append(val)

                if name == 'rho':
                    # val = getattr(pl_currents, 'ro')[ # It isn't right!
                    val = getattr(pl_currents, 'ro')[
                        self.__steps//2, self.__r_f:self.__r_t]
                    self.__data[name].append(val)

                if name == 'rho_beam':
                    val = ro_beam[self.__steps//2, self.__r_f:self.__r_t]
                    self.__data[name].append(val)

                if name in ['px', 'py', 'pz']:
                    val = getattr(pl_particles, name)[
                        self.__steps//2, self.__r_f:self.__r_t]
                    self.__data[name].append(val)
            
                val = 0

        # We use dump here to save data not only at the end of the simulation
        # window, but with some period too.
        # TODO: Do we really need this? Does it work right?
        if xi_plasma_layer % self.__saving_xi_period <= self.__xi_step_size / 2:
            self.dump(current_time)

        # We can save data and then clean the memory after
        # the end of a subwindow.
        if (xi_plasma_layer <= self.__xi_to and
            (xi_plasma_layer + self.__xi_step_size) >= self.__xi_to
        ):
            self.dump(current_time)

            for name in self.__data:
                self.__data[name] = []


    def conditions_check(self, current_time, xi_pl_layer):
        return (current_time % self.__output_period == 0 and
                xi_pl_layer <= self.__xi_from and xi_pl_layer >= self.__xi_to
        )

    def dump(self, current_time):
        # In case of colormaps, we reshape every data list except for xi list.
        if self.conditions_check(current_time, inf):
            data_for_saving = (self.__data).copy()

            size = len(self.__data['xi'])
            for name in self.__colormaps_names:
                data_for_saving[name] = np.reshape(
                    np.array(self.__data[name]), (size, -1)
                )

            # Merging the data along r and xi axes:
            if self.__merging_r > 1 or self.__merging_xi > 1:
                for name in self.__colormaps_names:
                    data_for_saving[name] = conv_2d(
                        data_for_saving, self.__merging_xi, self.__merging_r
                    )

            # TODO: If there is a file with the same name with important data 
            #       and we want to save data there, we should just add new data,
            #       not rewrite a file.
            Path('./diagnostics').mkdir(parents=True, exist_ok=True)
            if (
                'numbers' in self.__colormaps_type or
                'both' in self.__colormaps_type
            ):
                np.savez(f'./diagnostics/colormaps_{current_time:08.2f}.npz',
                    **data_for_saving)
            
            # Clean some memory. TODO: Better ways to do that?
            data_for_saving = 0

    def after_step_dt(self, *params):
        # We use this function to clean old data:
        for name in self.__data:
            self.__data[name] = []


class DiagnosticsTransverse:
    __allowed_colormaps = ['Ex', 'Ey', 'Ez', 'Bx', 'By', 'Bz', 'rho', 'rho_beam',
                           'px', 'py', 'pz', 'Phi']
                        # 'ni']
                    # TODO: add them and functionality!
    __allowed_colormaps_type = ['pictures']
    #TODO: add 'numbers' and 'both' and functionality

    def __init__(
        self, output_period=100, saving_xi_period=1000, colormaps='rho',
        colormaps_type='pictures'
    ):
        # It creates a list of string elements such as Ez, Ex, By...
        self.__colormaps_names = from_str_into_list(colormaps)
        for name in self.__colormaps_names:
            if name not in self.__allowed_colormaps:
                raise Exception(f'{name} value is not supported as a colormap.')

        # Output mode for the functions of xi:
        if colormaps_type in self.__allowed_colormaps_type:
            self.__colormaps_type = colormaps_type
        else:
            raise Exception(f"{colormaps_type} type of colormap diagnostics" +
                             "is not supported.")

        # Set time periodicity of detailed output:
        self.__output_period = output_period
        self.__saving_xi_period = saving_xi_period

    def __repr__(self) -> str:
        return (
            f"DiagnosticsTransverse(output_period={self.__output_period}, " +
            f"saving_xi_period={self.__saving_xi_period}, " + 
            f"colormaps='{','.join(self.__colormaps_names)}', " +
            f"colormaps_type='{self.__colormaps_type}'"
        )

    def pull_config(self, config: Config):
        """Pulls a config to get all required parameters."""
        # Here we check if the output period is less than the time step size.
        # In that case each time step is diagnosed. The first time step is
        # always diagnosed. And we check if period % time_step_size = 0,
        # because otherwise it won't diagnosed anything.
        self.__time_step_size = config.getfloat('time-step')
        self.__xi_step_size   = config.getfloat('xi-step')

        if self.__output_period < self.__time_step_size:
            self.__output_period = self.__time_step_size

        if self.__saving_xi_period < self.__xi_step_size:
            self.__saving_xi_period = self.__xi_step_size

        if self.__output_period % self.__time_step_size != 0:
            raise Exception(
                "The diagnostics will not work because " +
                f"{self.__output_period} % {self.__time_step_size} != 0"
            )

    def after_step_dxi(
        self, current_time, xi_plasma_layer, pl_particles, pl_fields,
        pl_currents, ro_beam
    ):
        if self.conditions_check(current_time, xi_plasma_layer):
            # For xy diagnostics we save files to a file or plot a picture.
            if (
                'pictures' in self.__colormaps_type or
                'both' in self.__colormaps_type
            ):
                Path('./diagnostics').mkdir(
                    parents=True, exist_ok=True
                )
                for name in self.__colormaps_names:
                    fname = f'{name}_{xi_plasma_layer:+09.2f}.png'
                
                    # if name in ['Ex', 'Ey', 'Ez', 'Bx', 'By', 'Bz', 'Phi']:
                    #     val = getattr(pl_fields, name)[
                    #         self.__steps//2, self.__r_f:self.__r_t]
                    #     self.__data[name].append(val)

                    if name == 'rho':
                        plt.imsave(
                            './diagnostics/' + fname,
                            getattr(pl_currents, 'ro').T, origin='lower',
                            vmin=-0.1, vmax=0.1, cmap='bwr'
                        )

                    # if name == 'rho_beam':
                    #     val = ro_beam[self.__steps//2, self.__r_f:self.__r_t]
                    #     self.__data[name].append(val)

                    # if name in ['px', 'py', 'pz']:
                    #     val = getattr(pl_particles, name)[
                    #         self.__steps//2, self.__r_f:self.__r_t]
                    #     self.__data[name].append(val)

    def conditions_check(self, current_time, xi_pl_layer):
        return (
            current_time % self.__output_period == 0 and
            xi_pl_layer  % self.__saving_xi_period <= self.__xi_step_size / 2
        )
    
    def dump(self, current_time):
        pass
    
    def after_step_dt(self, *params):
        pass


class SaveRunState:
    def __init__(self, saving_period=1000., save_beam=False, save_plasma=False):
        self.__saving_period = saving_period
        self.__save_beam = bool(save_beam)
        self.__save_plasma = bool(save_plasma)

    def __repr__(self) -> str:
        return(f"SaveRunState(saving_period={self.__saving_period}, " +
            f"save_beam={self.__save_beam}, save_plasma={self.__save_plasma})")

    def pull_config(self, config: Config):
        self.__time_step_size = config.getfloat('time-step')

        if self.__saving_period < self.__time_step_size:
            self.__saving_period = self.__time_step_size

        # Important for saving arrays from GPU:
        self.__pu_type = config.get('processing-unit-type').lower()

    def after_step_dxi(self, *parameters):
        pass

    def dump(self, *parameeters):
        pass

    def after_step_dt(
        self, current_time, pl_particles, pl_fields, pl_currents, beam_drain
    ):
        # The run is saved if the current_time differs from a multiple
        # of the saving period by less then dt/2.
        if current_time % self.__saving_period <= self.__time_step_size / 2:
            Path('./run_states').mkdir(parents=True, exist_ok=True)

            if self.__save_beam:
                beam_drain.beam_buffer.save(
                    f'./run_states/beamfile_{current_time:08.2f}')

            if self.__save_plasma:
                # Important for saving arrays from GPU (is it really?)
                if self.__pu_type == 'gpu':
                    pl_particles = GPUArraysView(pl_particles)
                    pl_fields    = GPUArraysView(pl_fields)
                    pl_currents  = GPUArraysView(pl_currents)

                np.savez_compressed(
                    file=f'./run_states/plasmastate_{current_time:08.2f}',
                    x_offt=pl_particles.x_offt, y_offt=pl_particles.y_offt,
                    px=pl_particles.px, py=pl_particles.py, pz=pl_particles.pz,
                    Ex=pl_fields.Ex, Ey=pl_fields.Ey, Ez=pl_fields.Ez,
                    Bx=pl_fields.Bx, By=pl_fields.By, Bz=pl_fields.Bz,
                    Phi=pl_fields.Phi, ro=pl_currents.ro,
                    jx=pl_currents.jx, jy=pl_currents.jy, jz=pl_currents.jz)
