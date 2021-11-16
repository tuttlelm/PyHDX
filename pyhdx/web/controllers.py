import itertools
import sys
import sys
import urllib.request
import zipfile
from collections import namedtuple
from datetime import datetime
from io import StringIO, BytesIO
from pathlib import Path

import colorcet
import matplotlib as mpl
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
import panel as pn
import param
from proplot import to_hex
from skimage.filters import threshold_multiotsu

from pyhdx.config import cfg
from pyhdx.fileIO import read_dynamx, csv_to_protein, csv_to_dataframe, dataframe_to_stringio
from pyhdx.fitting import fit_rates_weighted_average, fit_rates_half_time_interpolate, get_bounds, fit_gibbs_global, \
    fit_gibbs_global_batch, PATIENCE, STOP_LOSS, EPOCHS, R1, R2, optimizer_defaults, RatesFitResult
from pyhdx.models import PeptideMasterTable, HDXMeasurement, array_intersection
from pyhdx.plot import dG_scatter_figure, ddG_scatter_figure, linear_bars_figure, \
    rainbowclouds_figure
from pyhdx.support import series_to_pymol, apply_cmap
from pyhdx.web.base import ControlPanel, DEFAULT_CLASS_COLORS
from pyhdx.web.opts import CmapOpts
from pyhdx.web.widgets import ASyncProgressBar, CallbackProgress


class DevTestControl(ControlPanel):

    header = 'Debug'

    _type = 'dev'

    debug_btn = param.Action(lambda self: self._action_debug(), label='Debug')

    test_btn = param.Action(lambda self: self._action_test(), label='Test')


    def _action_debug(self):
        filters = self.filters
        sources = self.sources
        views = self.views
        opts = self.opts
        opt = self.opts['dG']

        tables = self.sources['main'].tables

        self.parent.logger.info('Info log')
        self.parent.logger.debug('Debug log')

        print('break')

    def _action_test(self):
        view = self.views['coverage']
        df = view.get_data()

        print(df)

    @property
    def _layout(self):
        return [
            ('self', None),
        ]


class PeptideFileInputControl(ControlPanel):
    """
    This controller allows users to input .csv file (Currently only DynamX format) of 'state' peptide uptake data.
    Users can then choose how to correct for back-exchange and which 'state' and exposure times should be used for
    analysis.

    """

    _type = 'peptide_file_input'

    header = 'Peptide Input'

    input_files = param.List()

    be_mode = param.Selector(doc='Select method of back exchange correction', label='Back exchange correction method', objects=['FD Sample', 'Flat percentage'])
    fd_state = param.Selector(doc='State used to normalize uptake', label='FD State')
    fd_exposure = param.Selector(doc='Exposure used to normalize uptake', label='FD Exposure')
    exp_state = param.Selector(doc='State for selected experiment', label='Experiment State')
    exp_exposures = param.ListSelector(default=[], objects=[''], label='Experiment Exposures'
                                       , doc='Selected exposure time to use')

    be_percent = param.Number(28., bounds=(0, 100), doc='Global percentage of back-exchange',
                              label='Back exchange percentage')

    drop_first = param.Integer(1, bounds=(0, None), doc='Select the number of N-terminal residues to ignore.')
    ignore_prolines = param.Boolean(True, constant=True, doc='Prolines are ignored as they do not exchange D.')
    d_percentage = param.Number(95., bounds=(0, 100), doc='Percentage of deuterium in the labelling buffer',
                                label='Deuterium percentage')
    #fd_percentage = param.Number(95., bounds=(0, 100), doc='Percentage of deuterium in the FD control sample buffer',
    #                             label='FD Deuterium percentage')
    temperature = param.Number(293.15, bounds=(0, 373.15), doc='Temperature of the D-labelling reaction',
                               label='Temperature (K)')
    pH = param.Number(7.5, doc='pH of the D-labelling reaction, as read from pH meter',
                      label='pH read')
    #load_button = param.Action(lambda self: self._action_load(), doc='Load the selected files', label='Load Files')

    n_term = param.Integer(1, doc='Index of the n terminal residue in the protein. Can be set to negative values to '
                                  'accommodate for purification tags. Used in the determination of intrinsic rate of exchange')
    c_term = param.Integer(0, bounds=(0, None),
                           doc='Index of the c terminal residue in the protein. Used for generating pymol export script'
                               'and determination of intrinsic rate of exchange for the C-terminal residue')
    sequence = param.String('', doc='Optional FASTA protein sequence')
    dataset_name = param.String()
    add_dataset_button = param.Action(lambda self: self._action_add_dataset(), label='Add dataset',
                                doc='Parse selected peptides for further analysis and apply back-exchange correction')
    #dataset_list = param.ObjectSelector(default=[], label='Datasets', doc='Lists available datasets')

    def __init__(self, parent, **params):
        self._excluded = ['be_percent']
        super(PeptideFileInputControl, self).__init__(parent, **params)

        self.update_box()

        self._df = None  # Numpy array with raw input data (or is pd.Dataframe?)

    @property
    def src(self):
        return self.sources['main']

    @property
    def own_widget_names(self):
        return [name for name in self.widgets.keys() if name not in self._excluded]

    @property
    def _layout(self):
        return [('self', self.own_widget_names)]

    def make_dict(self):
        text_area = pn.widgets.TextAreaInput(name='Sequence (optional)', placeholder='Enter sequence in FASTA format', max_length=10000,
                                             width=300, height=100, height_policy='fixed', width_policy='fixed')
        return self.generate_widgets(
            input_files=pn.widgets.FileInput(multiple=True, name='Input files'),
            temperature=pn.widgets.FloatInput,
            #be_mode=pn.widgets.RadioButtonGroup,
            be_percent=pn.widgets.FloatInput,
            d_percentage=pn.widgets.FloatInput,
            #fd_percentage=pn.widgets.FloatInput,
            sequence=text_area)

    def make_list(self):
        excluded = ['be_percent']
        widget_list = [widget for name, widget, in self.widget_dict.items() if name not in excluded]

        return widget_list

    @param.depends('be_mode', watch=True)
    def _update_be_mode(self):
        # todo @tejas: Add test
        if self.be_mode == 'FD Sample':
            self._excluded = ['be_percent']
        elif self.be_mode == 'Flat percentage':
            self._excluded = ['fd_state', 'fd_exposure']

        self.update_box()

    @param.depends('input_files', watch=True)
    def _read_files(self):
        if self.input_files:
            combined_df = read_dynamx(*[StringIO(byte_content.decode('UTF-8')) for byte_content in self.input_files])
            self._df = combined_df

            self.parent.logger.info(
                f'Loaded {len(self.input_files)} file{"s" if len(self.input_files) > 1 else ""} with a total '
                f'of {len(self._df)} peptides')

        else:
            self._df = None

        self._update_fd_state()
        self._update_fd_exposure()
        self._update_exp_state()
        self._update_exp_exposure()

    def _update_fd_state(self):
        if self._df is not None:
            states = list(self._df['state'].unique())
            self.param['fd_state'].objects = states
            self.fd_state = states[0]
        else:
            self.param['fd_state'].objects = []

    @param.depends('fd_state', watch=True)
    def _update_fd_exposure(self):
        if self._df is not None:
            fd_entries = self._df[self._df['state'] == self.fd_state]
            exposures = list(np.unique(fd_entries['exposure']))
        else:
            exposures = []
        self.param['fd_exposure'].objects = exposures
        if exposures:
            self.fd_exposure = exposures[0]

    @param.depends('fd_state', 'fd_exposure', watch=True)
    def _update_exp_state(self):
        if self._df is not None:
            # Booleans of data entries which are in the selected control
            control_bools = np.logical_and(self._df['state'] == self.fd_state, self._df['exposure'] == self.fd_exposure)

            control_data = self._df[control_bools].to_records()
            other_data = self._df[~control_bools].to_records()

            intersection = array_intersection([control_data, other_data], fields=['start', 'end'])  # sequence?
            states = list(np.unique(intersection[1]['state']))
        else:
            states = []

        self.param['exp_state'].objects = states
        if states:
            self.exp_state = states[0] if not self.exp_state else self.exp_state

    @param.depends('exp_state', watch=True)
    def _update_exp_exposure(self):
        if self._df is not None:
            exp_entries = self._df[self._df['state'] == self.exp_state]
            exposures = list(np.unique(exp_entries['exposure']))
            exposures.sort()
        else:
            exposures = []

        self.param['exp_exposures'].objects = exposures
        self.exp_exposures = exposures

        if not self.dataset_name or self.dataset_name in self.param['exp_state'].objects:
            self.dataset_name = self.exp_state

        if not self.c_term and exposures:
            self.c_term = int(np.max(exp_entries['end']))

    def _datasets_updated(self, events):
        # Update datasets widget as datasets on parents change
        objects = list(self.parent.data_objects.keys())
        self.param['dataset_list'].objects = objects

    def _action_add_dataset(self):
        """Apply controls to :class:`~pyhdx.models.PeptideMasterTable` and set :class:`~pyhdx.models.HDXMeasurement`"""

        if self._df is None:
            self.parent.logger.info("No data loaded")
            return
        elif self.dataset_name in self.src.hdxm_objects.keys():
            self.parent.logger.info(f"Dataset name {self.dataset_name} already in use")
            return

        peptides = PeptideMasterTable(self._df, d_percentage=self.d_percentage,
                                      drop_first=self.drop_first, ignore_prolines=self.ignore_prolines)
        if self.be_mode == 'FD Sample':
            control_0 = None # = (self.zero_state, self.zero_exposure) if self.zero_state != 'None' else None
            peptides.set_control((self.fd_state, self.fd_exposure), control_0=control_0)
        elif self.be_mode == 'Flat percentage':
            # todo @tejas: Add test
            peptides.set_backexchange(self.be_percent)

        data = peptides.get_state(self.exp_state)
        exp_bools = data['exposure'].isin(self.exp_exposures)
        data = data[exp_bools]

        #todo temperature ph kwarg for series
        hdxm = HDXMeasurement(data, c_term=self.c_term, n_term=self.n_term, sequence=self.sequence,
                              name=self.dataset_name, temperature=self.temperature, pH=self.pH)

        self.src.add(hdxm, self.dataset_name)
        self.parent.logger.info(f'Loaded dataset {self.dataset_name} with experiment state {self.exp_state} '
                                f'({len(hdxm)} timepoints, {len(hdxm.coverage)} peptides each)')
        self.parent.logger.info(f'Average coverage: {hdxm.coverage.percent_coverage:.3}%, '
                                f'Redundancy: {hdxm.coverage.redundancy:.2}')

    def _action_remove_datasets(self):
        raise NotImplementedError('Removing datasets not implemented')
        for name in self.dataset_list:
            self.parent.datasets.pop(name)

        self.parent.param.trigger('datasets')  # Manual trigger as key assignment does not trigger the param


class InitialGuessControl(ControlPanel):
    """
    This controller allows users to derive initial guesses for D-exchange rate from peptide uptake data.
    """

    _type = 'initial_guess'

    header = 'Initial Guesses'
    fitting_model = param.Selector(default='Half-life (λ)', objects=['Half-life (λ)', 'Association'],
                                   doc='Choose method for determining initial guesses.')
    dataset = param.Selector(default='', doc='Dataset to apply bounds to', label='Dataset (for bounds)')
    global_bounds = param.Boolean(default=False, doc='Set bounds globally across all datasets')
    lower_bound = param.Number(0., doc='Lower bound for association model fitting')
    upper_bound = param.Number(0., doc='Upper bound for association model fitting')
    guess_name = param.String(default='Guess_1', doc='Name for the initial guesses')
    do_fit1 = param.Action(lambda self: self._action_fit(), label='Calculate Guesses', doc='Start initial guess fitting',
                           constant=True)

    bounds = param.Dict({}, doc='Dictionary which stores rate fitting bounds', precedence=-1)

    def __init__(self, parent, **params):
        self.pbar1 = ASyncProgressBar()  #tqdm? https://github.com/holoviz/panel/pull/2079
        self.pbar2 = ASyncProgressBar()
        self._excluded = ['lower_bound', 'upper_bound', 'global_bounds', 'dataset']
        super(InitialGuessControl, self).__init__(parent, **params)
        self.src.param.watch(self._parent_hdxm_objects_updated, ['hdxm_objects'])  #todo refactor

        self.update_box()

        self._guess_names = {}

    @property
    def src(self):
        return self.sources['main']

    @property
    def _layout(self):
        return [
            ('self', self.own_widget_names),
        ]

    @property  # todo base class
    def own_widget_names(self):
        return [name for name in self.widgets.keys() if name not in self._excluded]

    def make_dict(self):
        widgets = self.generate_widgets(lower_bound=pn.widgets.FloatInput, upper_bound=pn.widgets.FloatInput)
        widgets.update(pbar1=self.pbar1.view, pbar2=self.pbar2.view)

        return widgets

    @param.depends('fitting_model', watch=True)
    def _fitting_model_updated(self):
        if self.fitting_model == 'Half-life (λ)':
            self._excluded = ['dataset', 'lower_bound', 'upper_bound', 'global_bounds']

        elif self.fitting_model in ['Association', 'Dissociation']:
            self._excluded = []

        self.update_box()

    @param.depends('global_bounds', watch=True)
    def _global_bounds_updated(self):
        if self.global_bounds:
            self.param['dataset'].constant = True
        else:
            self.param['dataset'].constant = False

    @param.depends('dataset', watch=True)
    def _dataset_updated(self):
        lower, upper = self.bounds[self.dataset]
        self.lower_bound = lower
        self.upper_bound = upper

    @param.depends('lower_bound', 'upper_bound', watch=True)
    def _bounds_updated(self):
        if not self.global_bounds:
            self.bounds[self.dataset] = (self.lower_bound, self.upper_bound)

    def _parent_hdxm_objects_updated(self, *events):
        if len(self.src.hdxm_objects) > 0:
            self.param['do_fit1'].constant = False

        # keys to remove:
        for k in self.bounds.keys() - self.src.hdxm_objects.keys():
            self.bounds.pop(k)
        # keys to add:
        for k in self.src.hdxm_objects.keys() - self.bounds.keys():
            self.bounds[k] = get_bounds(self.src.hdxm_objects[k].timepoints)

        options = list(self.src.hdxm_objects.keys())
        self.param['dataset'].objects = options
        if not self.dataset:
            self.dataset = options[0]

    def add_fit_result(self, future):
        name = self._guess_names.pop(future.key)

        results = future.result()
        result_obj = RatesFitResult(results)
        self.src.add(result_obj, name)

        self.param['do_fit1'].constant = False
        self.widgets['do_fit1'].loading = False

    def _action_fit(self):
        if len(self.src.hdxm_objects) == 0: # (Should be impossible as button is locked)
            self.parent.logger.info('No datasets loaded')
            return

        src = self.sources['main']  # todo property base class?

        if self.guess_name in itertools.chain(src.rate_results.keys(), self._guess_names.values()):
            self.parent.logger.info(f"Guess with name {self.guess_name} already in use")
            return

        self.parent.logger.info('Started initial guess fit')
        self.param['do_fit1'].constant = True
        self.widgets['do_fit1'].loading = True

        num_samples = len(self.src.hdxm_objects)
        if self.fitting_model.lower() in ['association', 'dissociation']:
            if self.global_bounds:
                bounds = [(self.lower_bound, self.upper_bound)]*num_samples
            else:
                bounds = self.bounds.values()

            futures = self.parent.client.map(fit_rates_weighted_average,
                                             self.self.src.hdxm_objects.values(), bounds, client='worker_client')
        elif self.fitting_model == 'Half-life (λ)':   # this is practically instantaneous and does not require dask
            futures = self.parent.client.map(fit_rates_half_time_interpolate, self.src.hdxm_objects.values())

        dask_future = self.parent.client.submit(lambda args: args, futures)  #combine multiple futures into one future
        self._guess_names[dask_future.key] = self.guess_name

        self.parent.future_queue.append((dask_future, self.add_fit_result))


class FitControl(ControlPanel):
    """
    This controller allows users to execute PyTorch fitting of the global data set.

    Currently, repeated fitting overrides the old result.
    """

    _type = 'fit'

    header = 'ΔG Fit'

    initial_guess = param.Selector(doc='Name of dataset to use for initial guesses.')

    fit_mode = param.Selector(default='Batch', objects=['Batch', 'Single'], constant=True)

    stop_loss = param.Number(STOP_LOSS, bounds=(0, None),
                             doc='Threshold loss difference below which to stop fitting.')
    stop_patience = param.Integer(PATIENCE, bounds=(1, None),
                                  doc='Number of epochs where stop loss should be satisfied before stopping.')
    learning_rate = param.Number(optimizer_defaults['SGD']['lr'], bounds=(0, None),
                                 doc='Learning rate parameter for optimization.')
    momentum = param.Number(optimizer_defaults['SGD']['momentum'], bounds=(0, None),
                            doc='Stochastic Gradient Descent momentum')
    nesterov = param.Boolean(optimizer_defaults['SGD']['nesterov'],
                             doc='Use Nesterov type of momentum for SGD')
    epochs = param.Integer(EPOCHS, bounds=(1, None),
                           doc='Maximum number of epochs (iterations.')
    r1 = param.Number(R1, bounds=(0, None), label='Regularizer 1 (peptide axis)',
                      doc='Value of the regularizer along residue axis.')

    r2 = param.Number(R2, bounds=(0, None), label='Regularizer 2 (sample axis)',
                      doc='Value of the regularizer along sample axis.', constant=True)

    fit_name = param.String("Gibbs_fit_1", doc="Name for for the fit result")

    do_fit = param.Action(lambda self: self._action_fit(), constant=True, label='Do Fitting',
                          doc='Start global fitting')

    def __init__(self, parent, **params):
        self.pbar1 = ASyncProgressBar() #tqdm?
        super(FitControl, self).__init__(parent, **params)

        self.src.param.watch(self._source_updated, ['updated'])

        self._current_jobs = 0
        self._max_jobs = 2  #todo config
        self._fit_names = {}

    def make_dict(self):
        widgets = self.generate_widgets()
        # widgets['progress'] = CallbackProgress()

        return widgets

    @property
    def src(self):
        return self.sources['main']

    def _source_updated(self, *events):
        objects = list(self.src.rate_results.keys())
        if objects:
            self.param['do_fit'].constant = False

        self._fit_mode_updated()

        self.param['initial_guess'].objects = objects
        if not self.initial_guess and objects:
            self.initial_guess = objects[0]

    @param.depends('fit_mode', watch=True)
    def _fit_mode_updated(self):
        if self.fit_mode == 'Batch' and len(self.src.hdxm_objects) > 1:
            self.param['r2'].constant = False
        else:
            self.param['r2'].constant = True

    def add_fit_result(self, future):
        #todo perhaps all these dfs should be in the future?
        name = self._fit_names.pop(future.key)
        result = future.result()
        self._current_jobs -= 1

        self.parent.logger.info(f'Finished PyTorch fit: {name}')

        # List of single fit results  (Currently outdated)
        if isinstance(result, list):
            self.parent.fit_results[name] = list(result)
            output_dfs = {fit_result.hdxm_set.name: fit_result.output for fit_result in result}
            df = pd.concat(output_dfs.values(), keys=output_dfs.keys(), axis=1)

            # create mse losses dataframe
            dfs = {}
            for single_result in result:
            # Determine mean squared errors per peptide, summed over timepoints
                mse = single_result.get_mse()
                mse_sum = np.sum(mse, axis=1)
                peptide_data = single_result.hdxm_set[0].data
                data_dict = {'start': peptide_data['start'], 'end': peptide_data['end'], 'total_mse': mse_sum}
                dfs[single_result.hdxm_set.name] = pd.DataFrame(data_dict)
            mse_df = pd.concat(dfs.values(), keys=dfs.keys(), axis=1)

            #todo d calc for single fits
            #todo losses for single fits

            # Create d_calc dataframe
            # -----------------------
            # todo needs cleaning up
            state_dfs = {}
            for single_result in result:
                tp_flat = single_result.hdxm_set.timepoints
                elem = tp_flat[np.nonzero(tp_flat)]

                time_vec = np.logspace(np.log10(elem.min()) - 1, np.log10(elem.max()), num=100, endpoint=True)
                d_calc_state = single_result(time_vec)  #shape Np x Nt
                hdxm = single_result.hdxm_set

                peptide_dfs = []
                pm_data = hdxm[0].data
                for d_peptide, pm_row in zip(d_calc_state, pm_data):
                    peptide_id = f"{pm_row['start']}_{pm_row['end']}"
                    data_dict = {'timepoints': time_vec, 'd_calc': d_peptide, 'start_end': [peptide_id] * len(time_vec)}
                    peptide_dfs.append(pd.DataFrame(data_dict))
                state_dfs[hdxm.name] = pd.concat(peptide_dfs, axis=0, ignore_index=True)

            d_calc_df = pd.concat(state_dfs.values(), keys=state_dfs.keys(), axis=1)


            # Create losses/epoch dataframe
            # -----------------------------
            losses_dfs = {fit_result.hdxm_set.name: fit_result.losses for fit_result in result}
            losses_df = pd.concat(losses_dfs.values(), keys=losses_dfs.keys(), axis=1)


        else:  # one batchfit result
            self.src.add(result, name)
            # self.parent.fit_results[name] = result  # todo this name can be changed by the time this is executed
            # df = result.output
            # # df.index.name = 'peptide index'
            #
            # # Create MSE losses df (per peptide, summed over timepoints)
            # # -----------------------
            # mse = result.get_mse()
            # dfs = {}
            # for mse_sample, hdxm in zip(mse, result.hdxm_set):
            #     peptide_data = hdxm[0].data
            #     mse_sum = np.sum(mse_sample, axis=1)
            #     # Indexing of mse_sum with Np to account for zero-padding
            #     data_dict = {'start': peptide_data['start'], 'end': peptide_data['end'], 'total_mse': mse_sum[:hdxm.Np]}
            #     dfs[hdxm.name] = pd.DataFrame(data_dict)
            #
            # mse_df = pd.concat(dfs.values(), keys=dfs.keys(), axis=1)
            #
            # self.parent.logger.info('Finished PyTorch fit')
            #
            # # Create d_calc dataframe
            # # -----------------------
            # tp_flat = result.hdxm_set.timepoints.flatten()
            # elem = tp_flat[np.nonzero(tp_flat)]
            #
            # time_vec = np.logspace(np.log10(elem.min()) - 1, np.log10(elem.max()), num=100, endpoint=True)
            # stacked = np.stack([time_vec for i in range(result.hdxm_set.Ns)])
            # d_calc = result(stacked)
            #
            # state_dfs = {}
            # for hdxm, d_calc_state in zip(result.hdxm_set, d_calc):
            #     peptide_dfs = []
            #     pm_data = hdxm[0].data
            #     for d_peptide, idx in zip(d_calc_state, pm_data.index):
            #         peptide_id = f"{pm_data.loc[idx, 'start']}_{pm_data.loc[idx, 'end']}"
            #         data_dict = {'timepoints': time_vec, 'd_calc': d_peptide, 'start_end': [peptide_id] * len(time_vec)}
            #         peptide_dfs.append(pd.DataFrame(data_dict))
            #     state_dfs[hdxm.name] = pd.concat(peptide_dfs, axis=0, ignore_index=True)
            # d_calc_df = pd.concat(state_dfs.values(), keys=state_dfs.keys(), axis=1)
            #
            # # Create losses/epoch dataframe
            # # -----------------------------
            # losses_df = result.losses.copy()
            # losses_df.columns = pd.MultiIndex.from_product(
            #     [['All states'], losses_df.columns],
            #     names=['state_name', 'quantity']
            # )

            self.parent.logger.info(
                f"Finished fitting in {len(result.losses)} epochs, final mean squared residuals is {result.mse_loss:.2f}")
            self.parent.logger.info(f"Total loss: {result.total_loss:.2f}, regularization loss: {result.reg_loss:.2f} "
                                    f"({result.regularization_percentage:.1f}%)")

        self.widgets['do_fit'].loading = False
        #self.widgets['progress'].max = self.epochs

        # self.parent.sources['dataframe'].add_df(df, 'global_fit', names=[name])
        # self.parent.sources['dataframe'].add_df(mse_df, 'peptides_mse', names=[name])
        # self.parent.sources['dataframe'].add_df(d_calc_df, 'd_calc', names=[name])
        # self.parent.sources['dataframe'].add_df(losses_df, 'losses', names=[name])

    def _action_fit(self):
        if self.fit_name in itertools.chain(self.src.dG_fits.keys(), self._fit_names.values()):
            self.parent.logger.info(f"Fit result with name {self.fit_name} already in use")
            return

        self.parent.logger.info('Started PyTorch fit')

        # self._current_jobs += 1
        # if self._current_jobs >= self._max_jobs:
        #     self.widgets['do_fit'].constant = True

        self.widgets['do_fit'].loading = True
        #self.widgets['progress'].max = self.epochs

        self.parent.logger.info(f'Current number of active jobs: {self._current_jobs}')
        if self.fit_mode == 'Batch':
            hdx_set = self.src.hdx_set
            rates_df = self.src.rate_results[self.initial_guess].output

            rates_guess = [rates_df[state]['rate'] for state in hdx_set.names]
            gibbs_guess = hdx_set.guess_deltaG(rates_guess)

            dask_future = self.parent.client.submit(fit_gibbs_global_batch, hdx_set, gibbs_guess, **self.fit_kwargs)
        else:
            data_objs = self.src.hdxm_objects.values()
            rates_df = self.src.rate_results[self.initial_guess].output
            gibbs_guesses = [data_obj.guess_deltaG(rates_df[data_obj.name]['rate']) for data_obj in data_objs]
            futures = self.parent.client.map(fit_gibbs_global, data_objs, gibbs_guesses, **self.fit_kwargs)

            # Combine list of futures into one future object
            # See https://github.com/dask/distributed/pull/560
            dask_future = self.parent.client.submit(lambda args: args, futures)

        self._fit_names[dask_future.key] = self.fit_name
        self.parent.future_queue.append((dask_future, self.add_fit_result))

    @property
    def fit_kwargs(self):
        fit_kwargs = dict(r1=self.r1, lr=self.learning_rate, momentum=self.momentum, nesterov=self.nesterov,
                          epochs=self.epochs, patience=self.stop_patience, stop_loss=self.stop_loss,)
                          #callbacks=[self.widgets['progress'].callback])
        if self.fit_mode == 'Batch':
            fit_kwargs['r2'] = self.r2

        return fit_kwargs


class ComparisonControl(ControlPanel):
    _type = 'comparison'

    header = 'Comparison (ΔΔG)'

    reference_state = param.Selector(
        doc='Which of the states to use as reference'
    )

    comparison_name = param.String(
        default='ddG_1',
        doc="Name for the comparison table"
    )

    add_comparison = param.Action(lambda self: self._action_add_comparison())

    def __init__(self, parent, **params):
        super().__init__(parent, **params)

        self.parent.filters['ddG_fit_select'].param.watch(self._source_updated, 'updated')
        self._df = None
        self._source_updated()  # todo filter source does not trigger updated when init

    @property
    def _layout(self):
        return [
            ('filters.ddG_fit_select', None),
            ('self', None)
        ]

    def get(self):
        df = self.filters['ddG_fit_select'].get()
        return df

    def _source_updated(self, *events):
        self._df = self.get()
        if self._df is not None:
            options = list(self._df.columns.unique(level=0))
            self.param['reference_state'].objects = options
            if self.reference_state is None and options:
                self.reference_state = options[0]

    def _action_add_comparison(self):
        current_df = self.parent.sources['main'].get('ddG_comparison')
        if current_df is not None and self.comparison_name in current_df.columns.get_level_values(level=0):
            self.parent.logger.info(f"Comparison name {self.comparison_name!r} already exists")
            return

        reference = self._df[self.reference_state]['deltaG']
        test = self._df.xs('deltaG', axis=1, level=1).drop(self.reference_state, axis=1)
        compare = test.subtract(reference, axis=0)

        columns = pd.MultiIndex.from_product(
            [[self.comparison_name], compare.columns, ['ddG']],
            names=['name', 'state', 'quantity'])
        compare.columns = columns

        if current_df is not None:
            new_df = pd.concat([current_df, compare], axis=1)
        else:
            new_df = compare

        self.parent.sources['main'].tables['ddG_comparison'] = new_df
        self.parent.sources['main'].param.trigger('tables')
        self.parent.sources['main'].updated = True


class ColorTransformControl(ControlPanel):
    """
    This controller allows users classify 'mapping' datasets and assign them colors.

    Coloring can be either in discrete categories or as a continuous custom color map.
    """

    _type = 'color_transform'

    header = 'Color Transform'

    # todo unify name for target field (target_data set)
    # When coupling param with the same name together there should be an option to exclude this behaviour
    quantity = param.Selector(label='Target Quantity')  # todo refactor cmapopt / color transform??
    # fit_ID = param.Selector()  # generalize selecting widgets based on selected table
    # quantity = param.Selector(label='Quantity')  # this is the lowest-level quantity of the multiindex df (filter??)

    current_color_transform = param.String()

    mode = param.Selector(default='Colormap', objects=['Colormap', 'Continuous', 'Discrete'],
                          doc='Choose color mode (interpolation between selected colors).')#, 'ColorMap'])
    num_colors = param.Integer(2, bounds=(1, 10), label='Number of colours',
                              doc='Number of classification colors.')
    library = param.Selector(default='pyhdx_default', objects=['pyhdx_default', 'user_defined', 'matplotlib', 'colorcet'])
    colormap = param.Selector()
    otsu_thd = param.Action(lambda self: self._action_otsu(), label='Otsu',
                            doc="Automatically perform thresholding based on Otsu's method.")
    linear_thd = param.Action(lambda self: self._action_linear(), label='Linear',
                              doc='Automatically perform thresholding by creating equally spaced sections.')
    #log_space = param.Boolean(False,
    #                          doc='Boolean to set whether to apply colors in log space or not.')
    #apply = param.Action(lambda self: self._action_apply())
    no_coverage = param.Color(default='#8c8c8c', doc='Color to use for regions of no coverage')

    live_preview = param.Boolean(default=True, doc='Toggle live preview on/off')

    color_transform_name = param.String('', doc='Name for the color transform to add')
    apply_colormap = param.Action(lambda self: self._action_apply_colormap(), label='Update color transform')

    #show_thds = param.Boolean(True, label='Show Thresholds', doc='Toggle to show/hide threshold lines.')
    values = param.List(default=[], precedence=-1)
    colors = param.List(default=[], precedence=-1)

    def __init__(self, parent, **param):
        self._excluded = ['otsu_thd', 'num_colors']
        super(ColorTransformControl, self).__init__(parent, **param)

        # https://discourse.holoviz.org/t/based-on-a-select-widget-update-a-second-select-widget-then-how-to-link-the-latter-to-a-reactive-plot/917/8
        # update to proplot cmaps?
        cc_cmaps = sorted(colorcet.cm.keys())
        mpl_cmaps = sorted(set(plt.colormaps()) - set('cet_' + cmap for cmap in cc_cmaps))

        self._pyhdx_cmaps = {}  # Dict of pyhdx default colormaps
        self._user_cmaps = {}
        cmap_opts = [opt for opt in self.opts.values() if isinstance(opt, CmapOpts)]
        self.quantity_mapping = {}  # quantity: (cmap, norm)
        for opt in cmap_opts:
            cmap, norm = opt.cmap, opt.norm_scaled
            self._pyhdx_cmaps[cmap.name] = cmap
            field = {'deltaG': 'dG'}.get(opt.field, opt.field)  # rename to dG in fit output files
            self.quantity_mapping[field] = (cmap, norm)

        self.cmap_options = {
            'matplotlib': mpl_cmaps, # list or dicts
            'colorcet': cc_cmaps,
            'pyhdx_default': self._pyhdx_cmaps,
            'user_defined': self._user_cmaps
        }

        self._update_num_colors()
        self._update_num_values()
        self._update_library()

        quantity_options = [opt.name for opt in self.opts.values() if isinstance(opt, CmapOpts)]
        self.param['quantity'].objects = quantity_options
        if self.quantity is None:
            self.quantity = quantity_options[0]

        self.update_box()

    @property
    def src(self):
        return self.sources['main']

    @property
    def own_widget_names(self):
        """returns a list of names of widgets in self.widgets to be laid out in controller card"""

        initial_widgets = []
        for name in self.param:
            precedence = self.param[name].precedence
            if (precedence is None or precedence > 0) and name not in self._excluded + ['name']:
                initial_widgets.append(name)

        #todo control color / value fields with param.add_parameter function
        widget_names = initial_widgets + [f'value_{i}' for i in range(len(self.values))]
        if self.mode != 'Colormap':
            widget_names += [f'color_{i}' for i in range(len(self.colors))]
        return widget_names

    def make_dict(self):
        return self.generate_widgets(num_colors=pn.widgets.IntInput, current_color_transform=pn.widgets.StaticText)

    @property
    def _layout(self):
        return [
            ('self', self.own_widget_names),
                ]

    def get_selected_data(self):
        #todo rfu residues peptides should be expanded one more dim to add quantity column level on top
        if self.quantity is None:
            self.parent.logger.info("No quantity selected to derive colormap thresholds from")
            return None

        # get the table key corresponding to the selected cmap quantity
        table_key = next(iter((k for k in self.src.tables.keys() if k.startswith(self.quantity))))

        table = self.src.get(table_key)
        if table is None:
            self.parent.logger.info(f"Table corresponding to {self.quantity!r} is empty")
            return None

        field = self.opts[self.quantity].field
        df = table.xs(field, level=-1) if field != 'rfu' else table # todo fix when fixing rfu dataframe

        return df

    def get_values(self):
        """return numpy array with only the values from selected dataframe, nan omitted"""

        array = self.get_selected_data().to_numpy().flatten()
        values = array[~np.isnan(array)]

        return values

    def _action_otsu(self):
        if self.num_colors <= 1:
            return
        values = self.get_values() # todo check for no values
        if not values.size:
            return

        #func = np.log if self.log_space else lambda x: x  # this can have NaN when in log space
        func = lambda x: x
        thds = threshold_multiotsu(func(values), classes=self.num_colors)
        widgets = [widget for name, widget in self.widgets.items() if name.startswith('value')]
        for thd, widget in zip(thds[::-1], widgets):  # Values from high to low
            widget.start = None
            widget.end = None
            widget.value = thd #np.exp(thd) if self.log_space else thd
        self._update_bounds()

    def _action_linear(self):
        i = 1 if self.mode == 'Discrete' else 0
        values = self.get_values()
        if not values.size:
            return

        # if self.log_space:
        #     thds = np.logspace(np.log(np.min(values)), np.log(np.max(values)),
        #                        num=self.num_colors + i, endpoint=True, base=np.e)
        #else:
        thds = np.linspace(np.min(values), np.max(values), num=self.num_colors + i, endpoint=True)

        widgets = [widget for name, widget in self.widgets.items() if name.startswith('value')]
        for thd, widget in zip(thds[i:self.num_colors][::-1], widgets):
            # Remove bounds, set values, update bounds
            widget.start = None
            widget.end = None
            widget.value = thd
        self._update_bounds()

    def _action_apply_colormap(self):
        if self.quantity is None:
            return

        cmap, norm = self.get_cmap_and_norm()
        if cmap and norm:
            cmap.name = self.color_transform_name
            #with # aggregate and execute at once: #            with param.parameterized.discard_events(opt): ??
            opt = self.opts[self.quantity]
            opt.cmap = cmap
            opt.norm_scaled = norm  # perhaps setter for norm such that externally it behaves as a rescaled thingy?

            self.quantity_mapping[self.quantity] = (cmap, norm)
            self._user_cmaps[cmap.name] = cmap

    @param.depends('colormap', 'values', 'colors', watch=True)
    def _preview_updated(self):
        if self.live_preview:
            pass
            #self._action_apply_colormap()

    @param.depends('quantity', watch=True)
    def _quantity_updated(self):
        cmap, norm = self.quantity_mapping[self.quantity]

        # with .. # todo accumulate events?

        preview = self.live_preview

        self.live_preview = False
        self.mode = 'Colormap'

        lib = 'pyhdx_default' if cmap.name in self._pyhdx_cmaps.keys() else 'user_defined'
        self.library = lib
        self.no_coverage = to_hex(cmap.get_bad(), keep_alpha=False)
        self.colormap = cmap.name
        self.color_transform_name = cmap.name
        self.current_color_transform = cmap.name

        thds = [norm.vmax, norm.vmin]
        widgets = [widget for name, widget in self.widgets.items() if name.startswith('value')]
        for thd, widget in zip(thds, widgets):
            # Remove bounds, set values, update bounds
            widget.start = None
            widget.end = None
            widget.value = thd
        self._update_bounds()
        self.live_preview = preview

    def get_cmap_and_norm(self):
        norm_klass = mpl.colors.Normalize

        # if not self.log_space else mpl.colors.LogNorm
        # if self.colormap_name in self.cmaps['pyhdx_default']: # todo change
        #     self.parent.logger.info(f"Colormap name {self.colormap_name} already exists")
        #     return None, None

        if len(self.values) < 2:
            return None, None

        if self.mode == 'Discrete':
            if len(self.values) != len(self.colors) - 1:
                return None, None
            cmap = mpl.colors.ListedColormap(self.colors)
            norm = mpl.colors.BoundaryNorm(self.values[::-1], self.num_colors, extend='both') #todo refactor values to thd_values

        elif self.mode == 'Continuous':
            norm = norm_klass(vmin=np.min(self.values), vmax=np.max(self.values), clip=True)
            positions = norm(self.values[::-1])
            cmap = mpl.colors.LinearSegmentedColormap.from_list('custom_cmap', list(zip(positions, self.colors)))

        elif self.mode == 'Colormap':
            norm = norm_klass(vmin=np.min(self.values), vmax=np.max(self.values), clip=True)
            if self.library == 'matplotlib':
                cmap = mpl.cm.get_cmap(self.colormap)
            elif self.library == 'colorcet':
                cmap = getattr(colorcet, 'm_' + self.colormap)
            elif self.library == 'pyhdx_default':
                cmap = self._pyhdx_cmaps[self.colormap]
            elif self.library == 'user_defined':
                cmap = self._user_cmaps[self.colormap]

        cmap.name = self.color_transform_name
        cmap.set_bad(self.no_coverage)

        return cmap, norm

    @param.depends('library', watch=True)
    def _update_library(self):
        collection = self.cmap_options[self.library]
        options = collection if isinstance(collection, list) else list(collection.keys())
        self.param['colormap'].objects = options
        if self.colormap is None or self.colormap not in options:  # todo how can it not be in options?
            self.colormap = options[0]

    @param.depends('mode', watch=True)
    def _mode_updated(self):
        if self.mode == 'Discrete':
            self._excluded = ['library', 'colormap']
    #        self.num_colors = max(3, self.num_colors)
    #        self.param['num_colors'].bounds = (3, None)
        elif self.mode == 'Continuous':
            self._excluded = ['library', 'colormap', 'otsu_thd']
      #      self.param['num_colors'].bounds = (2, None)
        elif self.mode == 'Colormap':
            self._excluded = ['otsu_thd', 'num_colors']
            self.num_colors = 2

        #todo adjust add/ remove color widgets methods
        self.param.trigger('num_colors')
        self.update_box()

    @param.depends('num_colors', watch=True)
    def _update_num_colors(self):
        while len(self.colors) != self.num_colors:
            if len(self.colors) > self.num_colors:
                self._remove_color()
            elif len(self.colors) < self.num_colors:
                self._add_color()
        self.param.trigger('colors')

    @param.depends('num_colors', watch=True)
    def _update_num_values(self):
        diff = 1 if self.mode == 'Discrete' else 0
        while len(self.values) != self.num_colors - diff:
            if len(self.values) > self.num_colors - diff:
                self._remove_value()
            elif len(self.values) < self.num_colors - diff:
                self._add_value()

        self._update_bounds()
        self.param.trigger('values')
        self.update_box()

    def _add_value(self):
        # value widgets are ordered in decreasing order, ergo next value widget
        # starts with default value of previous value -1
        try:
            first_value = self.values[-1]
        except IndexError:
            first_value = 0

        default = float(first_value - 1)
        self.values.append(default)

        name = f'Threshold {len(self.values)}'
        key = f'value_{len(self.values) - 1}'   # values already populated, first name starts at 1
        widget = pn.widgets.FloatInput(name=name, value=default)
        self.widgets[key] = widget
        widget.param.watch(self._value_event, ['value'])

    def _remove_value(self):
        key = f'value_{len(self.values) - 1}'
        widget = self.widgets.pop(key)
        self.values.pop()

        [widget.param.unwatch(watcher) for watcher in widget.param._watchers]
        del widget

    def _add_color(self):
        try:
            default = DEFAULT_CLASS_COLORS[len(self.colors)]
        except IndexError:
            default = "#"+''.join(np.random.choice(list('0123456789abcdef'), 6))

        self.colors.append(default)

        key = f'color_{len(self.colors) - 1}'
        widget = pn.widgets.ColorPicker(value=default)

        self.widgets[key] = widget

        widget.param.watch(self._color_event, ['value'])

    def _remove_color(self):
        key = f'color_{len(self.colors) - 1}'
        widget = self.widgets.pop(key)
        self.colors.pop()
        [widget.param.unwatch(watcher) for watcher in widget.param._watchers]
        del widget

    def _color_event(self, *events):
        for event in events:
            idx = list(self.widgets.values()).index(event.obj)
            key = list(self.widgets.keys())[idx]
            widget_index = int(key.split('_')[1])
            # idx = list(self.colors_widgets).index(event.obj)
            self.colors[widget_index] = event.new

        self.param.trigger('colors')

        #todo param trigger colors????

    def _value_event(self, *events):
        """triggers when a single value gets changed"""
        for event in events:
            idx = list(self.widgets.values()).index(event.obj)
            key = list(self.widgets.keys())[idx]
            widget_index = int(key.split('_')[1])
            self.values[widget_index] = event.new

        self._update_bounds()
        self.param.trigger('values')

    def _update_bounds(self):
        #for i, widget in enumerate(self.values_widgets.values()):
        for i in range(len(self.values)):
            widget = self.widgets[f'value_{i}']
            if i > 0:
                key = f'value_{i-1}'
                prev_value = float(self.widgets[key].value)
                widget.end = np.nextafter(prev_value, prev_value - 1)
            else:
                widget.end = None

            if i < len(self.values) - 1:
                key = f'value_{i+1}'
                next_value = float(self.widgets[key].value)
                widget.start = np.nextafter(next_value, next_value + 1)
            else:
                widget.start = None


class ProteinControl(ControlPanel):

    _type = 'protein'

    header = 'Protein Control'

    input_mode = param.Selector(doc='Method of protein structure input', objects=['PDB File', 'RCSB Download'])
    file_binary = param.Parameter()
    rcsb_id = param.String(doc='RCSB ID of protein to download')
    load_structure = param.Action(lambda self: self._action_load_structure())

    def __init__(self, parent, **params):
        self._excluded = ['rcsb_id']
        super(ProteinControl, self).__init__(parent, **params)

        self.update_box()

    @property
    def _layout(self):
        return [('self', self.own_widget_names),  #always use this instead of none?
                ('filters.protein_src', None),
                ('filters.protein_select', None),
                ('filters.protein_cmap', None),
                ('views.protein', None)
                ]

    @property  #todo in baseclass?
    def own_widget_names(self):
        return [name for name in self.widgets.keys() if name not in self._excluded]

    def make_dict(self):
        return self.generate_widgets(file_binary=pn.widgets.FileInput(multiple=False, accept='.pdb'))

    @param.depends('input_mode', watch=True)
    def _update_input_mode(self):
        if self.input_mode == 'PDB File':
            self._excluded = ['rcsb_id']
        elif self.input_mode == 'RCSB Download':
            self._excluded = ['file_binary']

        #self.own_widget_names = [name for name in self.widgets.keys() if name not in excluded]
        self.update_box()

    def _action_load_structure(self):

        if self.input_mode == 'PDB File':
            pdb_string = self.file_binary.decode()

        elif self.input_mode == 'RCSB Download':
            if len(self.rcsb_id) != 4:
                self.parent.logger.info(f"Invalid RCSB pdb id: {self.rcsb_id}")
                return

            url = f'http://files.rcsb.org/download/{self.rcsb_id}.pdb'
            with urllib.request.urlopen(url) as response:
                pdb_string = response.read().decode()

        view = self.views['protein']
        view.object = pdb_string


class FileExportControl(ControlPanel):

    """
    <outdated docstring>
    This controller allows users to export and download datasets.

    All datasets can be exported as .txt tables.
    'Mappable' datasets (with r_number column) can be exported as .pml pymol script, which colors protein structures
    based on their 'color' column.

    """

    _type = 'file_export'

    header = "File Export"

    table = param.Selector(label='Target dataset', doc='Name of the dataset to export')
    export_format = param.Selector(default='csv', objects=['csv', 'pprint'],
                                   doc="Format of the exported tables."
                                       "'csv' is machine-readable, 'pprint' is human-readable format")

    def __init__(self, parent, **param):
        super(FileExportControl, self).__init__(parent, **param)
        self.sources['main'].param.watch(self._tables_updated, ['tables', 'updated'])  #todo make up your mind: trigger tables or updated?
        #self._tables_updated()  # todo shouldnt be necessary

    def make_dict(self):
        widgets = self.generate_widgets()

        widgets['export_tables'] = pn.widgets.FileDownload(
            label='Download table',
            callback=self.table_export_callback
        )
        widgets['export_pml'] = pn.widgets.FileDownload(
            label='Download pml scripts',
            callback=self.pml_export_callback,
        )
        widgets['export_colors'] = pn.widgets.FileDownload(
            label='Download colors',
            callback=self.color_export_callback,
        )

        widget_order = ['table', 'export_format', 'export_tables', 'export_pml', 'export_colors']
        final_widgets = {w: widgets[w] for w in widget_order}

        return final_widgets

    def _tables_updated(self, *events):
        options = list(self.sources['main'].tables.keys())
        self.param['table'].objects = options
        if not self.table and options:
            self.table = options[0]

    @property
    def _layout(self):
        return [
            ('self', None)
        ]

    @param.depends('table', 'export_format', watch=True)
    def _table_updated(self):

        ext = '.csv' if self.export_format == 'csv' else '.txt'
        self.widgets['export_tables'].filename = self.table + ext

        qty = self.table.split('_')[0]
        cmap_opts = {k: opt for k, opt in self.opts.items() if isinstance(opt, CmapOpts)}
        if qty in cmap_opts.keys():
            self.widgets['export_pml'].disabled = False
            self.widgets['export_colors'].disabled = False
            self.widgets['export_pml'].filename = self.table + '_pml_scripts.zip'
            self.widgets['export_colors'].filename = self.table + '_colors' + ext
        else:
            self.widgets['export_pml'].disabled = True
            self.widgets['export_colors'].disabled = True

    @pn.depends('table')  # param.depends?
    def table_export_callback(self):
        if self.table:
            df = self.sources['main'].tables[self.table]
            io = dataframe_to_stringio(df, fmt=self.export_format)
            return io
        else:
            return None

    @pn.depends('table')
    def pml_export_callback(self):
        if self.table:
            #todo check if table is valid for pml conversion

            color_df = self.get_color_df()

            bio = BytesIO()
            with zipfile.ZipFile(bio, 'w') as pml_zip:
                for col_name in color_df.columns:
                    name = col_name if isinstance(col_name, str) else '_'.join(col_name)
                    colors = color_df[col_name]
                    pml_script = series_to_pymol(colors)  # todo refactor pd_series_to_pymol?
                    pml_zip.writestr(name + '.pml', pml_script)

            bio.seek(0)
            return bio

    def get_color_df(self):
        df = self.sources['main'].tables[self.table]
        qty = self.table.split('_')[0]
        opt = self.opts[qty]
        cmap = opt.cmap
        norm = opt.norm
        if qty == 'dG':
            df = df.xs('deltaG', level=-1, axis=1)

        color_df = apply_cmap(df, cmap, norm)

        return color_df

    @pn.depends('table')
    def color_export_callback(self):
        if self.table:
            df = self.get_color_df()
            io = dataframe_to_stringio(df, fmt=self.export_format)
            return io
        else:
            return None


class FigureExportControl(ControlPanel):

    _type = 'figure_export'

    header = "Figure Export"

    figure = param.Selector(default='scatter', objects=['scatter', 'linear_bars', 'rainbowclouds'])

    reference = param.Selector(allow_None=True)

    figure_selection = param.Selector(label='Selection')

    figure_format = param.Selector(default='png', objects=['png', 'pdf', 'svg', 'eps'])

    ncols = param.Integer(
        default=2,
        label='Number of columns',
        bounds=(1, 4),
        doc="Number of columns in subfigure")

    aspect = param.Number(
        default=3.,
        label='Aspect ratio',
        doc="Subfigure aspect ratio"
    )

    width = param.Number(
        default=cfg.getfloat('plotting', 'page_width'),
        label='Figure width (mm)',
        bounds=(50, None),
        doc="""Width of the output figure"""
    )

    def __init__(self, parent, **param):
        self._excluded = []
        super(FigureExportControl, self).__init__(parent, **param)
        self.sources['main'].param.watch(self._figure_updated, ['tables', 'updated'])

        self._figure_updated()

    @property
    def _layout(self):
        return [('self', self.own_widget_names),  #TODO always use this instead of none?
                ]

    @property  #todo in baseclass?
    def own_widget_names(self):
        return [name for name in self.widgets.keys() if name not in self._excluded]

    def make_dict(self):
        widgets = self.generate_widgets()

        widgets['export_figure'] = pn.widgets.FileDownload(
            label='Download figure',
            callback=self.figure_export_callback,
        )

        widget_order = ['figure', 'reference', 'figure_selection', 'figure_format', 'ncols', 'aspect', 'width',
                        'export_figure']
        final_widgets = {w: widgets[w] for w in widget_order}

        return final_widgets

    @pn.depends('figure', watch=True)
    def _figure_updated(self, *events):
        # generalize more when other plot options are introduced
        if not self.figure:
            return

        if 'dG_fits' not in self.sources['main'].tables.keys():
            return

        if self.figure in ['scatter', 'linear_bars', 'rainbowclouds']:  # currently this is always true
            df = self.sources['main'].tables['dG_fits']
            options = list(df.columns.unique(level=0))
            self.param['figure_selection'].objects = options
            if not self.figure_selection:
                self.figure_selection = options[0]

            options = list(df.columns.unique(level=1))
            self.param['reference'].objects = [None] + options

            self._excluded = []

        if self.figure == 'scatter':
            self.aspect = cfg.getfloat('plotting', 'deltaG_aspect')  # todo refactor to dG
            self._excluded = []
        else:
            self.aspect = cfg.getfloat('plotting', f'{self.figure}_aspect')
            self._excluded = ['ncols']

        self.update_box()

    @pn.depends('figure_selection', watch=True)
    def _figure_selection_updated(self):  # selection is usually Fit ID
        df = self.sources['main'].tables['dG_fits'][self.figure_selection]
        options = list(df.columns.unique(level=0))
        self.param['reference'].objects = [None] + options
        if not self.reference and options:
            self.reference = options[0]

    @pn.depends('figure', 'figure_selection', 'figure_format', watch=True)
    def _figure_filename_updated(self):
        qty = 'dG' if self.reference is None else 'ddG'
        fname = f'{self.figure}_{qty}_{self.figure_selection}.{self.figure_format}'

        self.widgets['export_figure'].filename = fname

    @pn.depends('figure')
    def figure_export_callback(self):
        self.widgets['export_figure'].loading = True

        if not self.figure:
            return None

        if 'dG_fits' not in self.sources['main'].tables.keys():
            self.parent.logger.info("No ΔG fits results available")
            return None

        df = self.sources['main'].tables['dG_fits']
        sub_df = df[self.figure_selection]

        if self.figure == 'scatter':
            if self.reference is None:
                opts = self.opts['dG']
                fig, axes, cbars = dG_scatter_figure(sub_df, cmap=opts.cmap, norm=opts.norm, **self.figure_kwargs)

            else:
                opts = self.opts['ddG']
                fig, axes, cbar = ddG_scatter_figure(sub_df, reference=self.reference, cmap=opts.cmap, norm=opts.norm,
                                                     **self.figure_kwargs)
        elif self.figure == 'linear_bars':
            opts = self.opts['ddG'] if self.reference else self.opts['dG']
            fig, axes = linear_bars_figure(sub_df, reference=self.reference, cmap=opts.cmap, norm=opts.norm)
        elif self.figure == 'rainbowclouds':
            opts = self.opts['ddG'] if self.reference else self.opts['dG']
            fig, axes, cbar = rainbowclouds_figure(sub_df, reference=self.reference, cmap=opts.cmap, norm=opts.norm)

        bio = BytesIO()
        fig.savefig(bio, format=self.figure_format)
        bio.seek(0)

        self.widgets['export_figure'].loading = False

        return bio

    @property
    def figure_kwargs(self):
        kwargs = {
            'width': self.width,
            'aspect': self.aspect,
        }
        if self.figure == 'scatter':
            kwargs['ncols'] = self.ncols
        return kwargs


class SessionManagerControl(ControlPanel):
    _type = 'session_manager'

    header = 'Session Manager'

    session_file = param.Parameter()

    load_session = param.Action(lambda self: self._load_session())

    reset_session = param.Action(lambda self: self._reset_session())

    def make_dict(self):
        widgets = self.generate_widgets(session_file=pn.widgets.FileInput)

        widgets['export_session'] = pn.widgets.FileDownload(
            label='Export session',
            callback=self.export_session_callback,
            filename='PyHDX_session.zip'
        )

        names = ['session_file', 'load_session', 'export_session', 'reset_session']
        widgets = {name: widgets[name] for name in names}

        return widgets

    def export_session_callback(self):
        dt = datetime.today().strftime('%Y%m%d_%H%M')
        self.widgets['export_session'].filename = f'{dt}_PyHDX_session.zip'
        bio = BytesIO()
        with zipfile.ZipFile(bio, 'w') as session_zip:
            for name, table in self.sources['main'].tables.items():
                sio = dataframe_to_stringio(table)
                session_zip.writestr(name + '.csv', sio.getvalue())

        bio.seek(0)
        return bio

    def _load_session(self):
        if self.session_file is None:
            return None

        if sys.getsizeof(self.session_file) > 5.e8:
            self.parent.logger.info("Uploaded file is too large, maximum is 500 MB")
            return None

        bio = BytesIO(self.session_file)

        session_zip = zipfile.ZipFile(bio)
        session_zip.printdir()
        names = set(session_zip.namelist())
        accepted_names = {'rfu_residues.csv', 'rates.csv', 'peptides.csv', 'dG_fits.csv', 'ddG_comparison.csv'}

        self._reset()
        src = self.sources['main']
        for name in names & accepted_names:
            bio = BytesIO(session_zip.read(name))
            df = csv_to_dataframe(bio)
            src.tables[name.split('.')[0]] = df

        src.param.trigger('tables')
        src.updated = True

    def _reset_session(self):
        self._reset()
        self.sources['main'].updated = True

        # todo for ctrl in cotrollers ctrl.reset()?

    def _reset(self):
        src = self.sources['main']
        with param.parameterized.discard_events(src):
            src.hdxm_objects = {}
            src.rate_results = {}
            src.dG_fits = {}

        src.tables = {}  # are there any dependies on this?


class GraphControl(ControlPanel):
    _type = 'graph'

    header = 'Graph Control'

    spin = param.Boolean(default=False, doc='Spin the protein object')

    state = param.Selector(doc="Name of the currently selected state")
    fit_id = param.Selector(doc="Name of the currently selected fit ID")
    peptide_index = param.Selector(doc="Index of the currently selected peptide")

    def __init__(self, parent, **params):
        super(GraphControl, self).__init__(parent, **params)
        #source = self.sources['dataframe']
        self.src.param.watch(self._hdxm_objects_updated, 'hdxm_objects')

        # widget = self.widgets['state']
        # target = self.filters['dG_fit_select'].selectors[0]
        # widget.link(target, value='value')

    @property
    def src(self):
        return self.sources['main']

    def make_dict(self):
        widgets = {
            'general': pn.pane.Markdown('### General'),
            'coverage': pn.pane.Markdown('### Coverage'),
            'rfu': pn.pane.Markdown('### RFU'),
            'rates': pn.pane.Markdown('### Rates'),
            'dG': pn.pane.Markdown('### ΔG'),
            'ddG': pn.pane.Markdown('### ΔΔG'),
            #'debugging': pn.pane.Markdown('### Debugging'),

        }

        return {**widgets, **self.generate_widgets()}

    @property
    def _layout(self):
        return [
            ('self', 'coverage'),
            ('filters.coverage_select', None),
            ('self', 'rfu'),
            ('filters.rfu_select', None),
            ('self', 'rates'),
            ('filters.rates_select', None),
            ('self', 'dG'),
            ('filters.dG_fit_select', None),
            ('self', 'ddG'),
            ('filters.ddG_comparison_select', None),

        ]

    def _hdxm_objects_updated(self, *events):
        options = list(self.src.hdxm_objects.keys())
        self.param['state'].objects = options
        if self.state is None and options:
            self.state = options[0]

    def _source_updated(self, *events):
        source = self.sources['dataframe']
        table = source.get('global_fit')
        fit_id_options = list(table.columns.get_level_values(0).unique())
        self.param['fit_id'].objects = fit_id_options
        if not self.fit_id and fit_id_options:
            self.fit_id = fit_id_options[0]

        table = source.get('peptides')
        state_name_options = list(table.columns.get_level_values(0).unique())

        self.param['state_name'].objects = state_name_options
        if not self.state_name and state_name_options:
            self.state_name = state_name_options[0]

    #@param.depends('state_name', watch=True)
    def _update_state_name(self):
        #https://param.holoviz.org/reference.html#param.parameterized.batch_watch

        dwarfs = ['coverage_state_name', 'coverage_mse_state_name', 'peptide_d_exp_state_name', 'peptide_d_calc_state_name',
                  'deltaG_state_name', 'rates_state_name', 'ngl_state_name']  # there really are 7

        # one filter to rule them all, one filter to find them,
        # one filter to bring them all, and in the darkness bind them;
        # in the Land of Mordor where the shadows lie.
        for dwarf in dwarfs:
            filt = self.filters[dwarf]
            filt.value = self.state_name

        # If current fit result was done as single, also update the state for the losses graph
        losses_filt = self.filters['losses_state_name']
        if self.state_name in losses_filt.param['value'].objects:
            losses_filt.value = self.state_name


        # Update possible choices for peptide selection depending on selected state
        source = self.sources['dataframe']
        table = source.get('peptides')
        unique_vals = table[self.state_name]['start_end'].unique()
        peptide_options = list(range(len(unique_vals)))
        self.param['peptide_index'].objects = peptide_options
        if self.peptide_index is not None and peptide_options:
            self.peptide_index = peptide_options[0]

    # @param.depends('fit_id', watch=True)
    # def _update_fit_id(self):
    #     elves = ['coverage_mse_fit_id', 'peptide_d_calc_fit_id', 'deltaG_fit_id', 'losses_fit_id']
    #     for elf in elves:
    #         filt = self.filters[elf]
    #         filt.value = self.fit_id
    #
    #     # perhaps this is faster?
    #     # widget = self.widget.clone()
    #     # self.widget.link(widget, value='value', bidirectional=True)
    #
    # @param.depends('peptide_index', watch=True)
    # def _update_peptide_index(self):
    #     hobbits = ['peptide_d_exp_select', 'peptide_d_calc_select']
    #     for hobbit in hobbits:
    #         filt = self.filters[hobbit]
    #         filt.value = self.peptide_index
